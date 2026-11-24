import csv
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction
from PIL import Image

from app.image_utils import convert_image_to_jpeg


@dataclass(frozen=True)
class ImportImage:
    filename: str
    path: Path
    description: str


class Command(BaseCommand):
    help = "Import a folder of images and keyword metadata into S3 and Postgres."

    def add_arguments(self, parser):
        batch_dir = Path(settings.BASE_DIR).parent / "image_import"
        parser.add_argument(
            "--images-dir",
            type=Path,
            default=batch_dir / "images",
            help="Folder containing the images (default: image_import/images).",
        )
        parser.add_argument(
            "--manifest",
            type=Path,
            default=batch_dir / "manifest.csv",
            help="CSV with filename and keywords columns.",
        )
        parser.add_argument(
            "--results",
            type=Path,
            default=batch_dir / "import_results.csv",
            help="Import state file used to report results and safely resume.",
        )
        parser.add_argument(
            "--username",
            help="Existing Wallify username; omit to leave images unlinked to a user.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate the manifest and images without uploading or writing to the database.",
        )

    def handle(self, *args, **options):
        images_dir = options["images_dir"].resolve()
        manifest_path = options["manifest"].resolve()
        rows = self._read_manifest(manifest_path, images_dir)
        self._validate_images(rows)

        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry run successful: {len(rows)} image(s) are ready to import."
                )
            )
            return

        username = (options["username"] or "").strip() or None
        if username:
            self._validate_username(username)
        results_path = options["results"].resolve()
        results = self._read_results(results_path)

        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME,
        )

        succeeded = 0
        skipped = 0
        failed = 0
        for row in rows:
            try:
                with row.path.open("rb") as source_file:
                    jpeg_content = convert_image_to_jpeg(source_file)
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                self.stderr.write(f"Failed {row.filename}: could not convert image: {error}")
                self._save_result(
                    results,
                    results_path,
                    username,
                    row,
                    "",
                    "",
                    "failed",
                    str(error),
                )
                failed += 1
                continue

            content_hash = hashlib.sha256(jpeg_content).hexdigest()
            result_id = (username or "", row.filename, row.description, content_hash)
            existing_result = results.get(result_id)
            if existing_result and existing_result["status"] == "success":
                self.stdout.write(f"Skipped {row.filename}: already imported.")
                skipped += 1
                continue

            image_key = (
                existing_result["image_key"]
                if existing_result and existing_result["image_key"]
                else f"{uuid.uuid4().hex}.jpg"
            )
            if existing_result and self._database_image_exists(
                image_key, row.description, username
            ):
                self._save_result(
                    results,
                    results_path,
                    username,
                    row,
                    content_hash,
                    image_key,
                    "success",
                    "",
                )
                self.stdout.write(f"Skipped {row.filename}: already imported.")
                skipped += 1
                continue

            self._save_result(
                results,
                results_path,
                username,
                row,
                content_hash,
                image_key,
                "pending",
                "",
            )
            s3_key = f"images/{image_key}"
            try:
                s3.put_object(
                    Bucket=settings.AWS_STORAGE_BUCKET_NAME,
                    Key=s3_key,
                    Body=jpeg_content,
                    ContentType="image/jpeg",
                )
            except (BotoCoreError, ClientError) as error:
                self.stderr.write(f"Failed {row.filename}: S3 upload failed: {error}")
                self._save_result(
                    results,
                    results_path,
                    username,
                    row,
                    content_hash,
                    image_key,
                    "failed",
                    str(error),
                )
                failed += 1
                continue

            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO images_table (image_key, description, "user")
                            VALUES (%s, %s, %s)
                            """,
                            [image_key, row.description, username],
                        )
            except DatabaseError as error:
                try:
                    s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=s3_key)
                except (BotoCoreError, ClientError) as cleanup_error:
                    self.stderr.write(
                        f"Cleanup failed for {s3_key}; the S3 object may remain: {cleanup_error}"
                    )
                self.stderr.write(f"Failed {row.filename}: database insert failed: {error}")
                self._save_result(
                    results,
                    results_path,
                    username,
                    row,
                    content_hash,
                    image_key,
                    "failed",
                    str(error),
                )
                failed += 1
                continue

            self._save_result(
                results,
                results_path,
                username,
                row,
                content_hash,
                image_key,
                "success",
                "",
            )
            self.stdout.write(self.style.SUCCESS(f"Imported {row.filename} as {image_key}"))
            succeeded += 1

        self.stdout.write(
            f"Import finished: {succeeded} uploaded, {skipped} skipped, {failed} failed."
        )
        if failed:
            raise CommandError("Some images were not imported; see errors above.")

    def _read_manifest(self, manifest_path, images_dir):
        if not images_dir.is_dir():
            raise CommandError(f"Images folder does not exist: {images_dir}")
        if not manifest_path.is_file():
            raise CommandError(f"Manifest does not exist: {manifest_path}")

        rows = []
        seen_filenames = set()
        try:
            with manifest_path.open(newline="", encoding="utf-8-sig") as manifest_file:
                reader = csv.DictReader(manifest_file)
                if reader.fieldnames is None or not {"filename", "keywords"}.issubset(
                    reader.fieldnames
                ):
                    raise CommandError("Manifest must have filename and keywords columns.")

                for line_number, data in enumerate(reader, start=2):
                    filename = (data.get("filename") or "").strip()
                    keywords = [
                        keyword.strip()
                        for keyword in (data.get("keywords") or "").split(",")
                        if keyword.strip()
                    ]
                    if not filename or not keywords:
                        raise CommandError(
                            f"Manifest line {line_number} needs a filename and at least one keyword."
                        )
                    if Path(filename).name != filename or filename in {".", ".."}:
                        raise CommandError(
                            f"Manifest line {line_number} must use a filename, not a path."
                        )
                    if filename in seen_filenames:
                        raise CommandError(
                            f"Manifest contains duplicate filename on line {line_number}: {filename}"
                        )

                    image_path = (images_dir / filename).resolve()
                    if image_path.parent != images_dir:
                        raise CommandError(
                            f"Manifest line {line_number} points outside the images folder."
                        )
                    if not image_path.is_file():
                        raise CommandError(
                            f"Image listed on line {line_number} was not found: {filename}"
                        )

                    rows.append(
                        ImportImage(
                            filename=filename,
                            path=image_path,
                            description=", ".join(keywords),
                        )
                    )
                    seen_filenames.add(filename)
        except (OSError, csv.Error, UnicodeError) as error:
            raise CommandError(f"Could not read manifest: {error}") from error

        if not rows:
            raise CommandError("Manifest contains no image rows.")
        return rows

    def _read_results(self, results_path):
        if not results_path.exists():
            return {}

        results = {}
        try:
            with results_path.open(newline="", encoding="utf-8-sig") as results_file:
                reader = csv.DictReader(results_file)
                required_fields = {
                    "username",
                    "filename",
                    "keywords",
                    "sha256",
                    "image_key",
                    "status",
                    "error",
                }
                if reader.fieldnames is None or not required_fields.issubset(
                    reader.fieldnames
                ):
                    raise CommandError("Import results file has an invalid header.")

                for result in reader:
                    result_id = (
                        result["username"],
                        result["filename"],
                        result["keywords"],
                        result["sha256"],
                    )
                    results[result_id] = result
        except (OSError, csv.Error, UnicodeError) as error:
            raise CommandError(f"Could not read results file: {error}") from error
        return results

    def _save_result(
        self,
        results,
        results_path,
        username,
        row,
        content_hash,
        image_key,
        status,
        error,
    ):
        result_id = (username or "", row.filename, row.description, content_hash)
        results[result_id] = {
            "username": username or "",
            "filename": row.filename,
            "keywords": row.description,
            "sha256": content_hash,
            "image_key": image_key,
            "status": status,
            "error": error,
        }
        results_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                newline="",
                encoding="utf-8",
                dir=results_path.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                writer = csv.DictWriter(
                    temporary_file,
                    fieldnames=[
                        "username",
                        "filename",
                        "keywords",
                        "sha256",
                        "image_key",
                        "status",
                        "error",
                    ],
                )
                writer.writeheader()
                writer.writerows(results.values())
            os.replace(temporary_path, results_path)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()

    def _validate_images(self, rows):
        invalid_images = []
        for row in rows:
            try:
                with row.path.open("rb") as source_file:
                    convert_image_to_jpeg(source_file)
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                invalid_images.append(f"{row.filename}: {error}")

        if invalid_images:
            details = "\n".join(invalid_images)
            raise CommandError(f"Image validation failed; nothing was uploaded:\n{details}")

    def _validate_username(self, username):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM wallify_users WHERE username = %s",
                [username],
            )
            if cursor.fetchone() is None:
                raise CommandError(f"Wallify username does not exist: {username}")

    def _database_image_exists(self, image_key, description, username):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM images_table
                WHERE image_key = %s AND description = %s
                    AND "user" IS NOT DISTINCT FROM %s
                """,
                [image_key, description, username],
            )
            return cursor.fetchone() is not None

import io
import tempfile
from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import RequestFactory, SimpleTestCase, override_settings
from PIL import Image

from .views import upload_image


class UploadImageTests(SimpleTestCase):
    def test_webp_upload_is_converted_to_jpeg_in_images_folder(self):
        source = io.BytesIO()
        Image.new("RGBA", (16, 16), (255, 0, 0, 0)).save(
            source, format="WEBP", lossless=True
        )
        uploaded_file = SimpleUploadedFile(
            "same-name.webp", source.getvalue(), content_type="image/webp"
        )
        request = RequestFactory().post(
            "/upload_image/",
            {"description": "test image", "image": uploaded_file},
        )
        request.session = {"username": "test-user"}

        s3 = MagicMock()
        database = MagicMock()
        cursor_context = MagicMock()
        cursor = cursor_context.__enter__.return_value
        database.cursor.return_value = cursor_context
        with (
            patch("app.views.boto3.client", return_value=s3),
            patch("app.views.connection", database),
        ):
            response = upload_image(request)

        self.assertEqual(response.status_code, 302)
        s3_upload = s3.put_object.call_args.kwargs
        stored_key = cursor.execute.call_args.args[1][0]
        self.assertRegex(stored_key, r"^[0-9a-f]{32}\.jpg$")
        self.assertEqual(s3_upload["Key"], f"images/{stored_key}")
        self.assertEqual(s3_upload["ContentType"], "image/jpeg")

        with Image.open(io.BytesIO(s3_upload["Body"])) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertGreaterEqual(min(result.convert("RGB").getpixel((8, 8))), 250)


class ImportImagesCommandTests(SimpleTestCase):
    def _create_batch(self, batch_dir):
        images_dir = batch_dir / "images"
        images_dir.mkdir()
        Image.new("RGB", (4, 4), (30, 60, 90)).save(
            images_dir / "sample.webp", format="WEBP"
        )
        manifest = batch_dir / "manifest.csv"
        manifest.write_text(
            'filename,keywords\nsample.webp,"cat, black cat"\n',
            encoding="utf-8",
        )
        return images_dir, manifest

    def _run_command(self, images_dir, manifest, cursor, s3, username="test-user"):
        database = MagicMock()
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        database.cursor.return_value = cursor_context
        transaction = MagicMock()
        transaction.atomic.return_value = nullcontext()
        command_args = [
            "--images-dir",
            str(images_dir),
            "--manifest",
            str(manifest),
            "--results",
            str(manifest.parent / "import_results.csv"),
        ]
        if username:
            command_args.extend(["--username", username])
        with (
            patch("app.management.commands.import_images.connection", database),
            patch("app.management.commands.import_images.boto3.client", return_value=s3),
            patch("app.management.commands.import_images.transaction", transaction),
            override_settings(AWS_STORAGE_BUCKET_NAME="test-bucket"),
        ):
            call_command(
                "import_images",
                *command_args,
                stdout=StringIO(),
                stderr=StringIO(),
            )

    def test_import_command_uploads_jpeg_and_saves_matching_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images_dir, manifest = self._create_batch(Path(temp_dir))
            cursor = MagicMock()
            cursor.fetchone.return_value = (1,)
            s3 = MagicMock()

            self._run_command(images_dir, manifest, cursor, s3)

        s3_upload = s3.put_object.call_args.kwargs
        stored_key = cursor.execute.call_args_list[-1].args[1][0]
        self.assertRegex(stored_key, r"^[0-9a-f]{32}\.jpg$")
        self.assertEqual(s3_upload["Key"], f"images/{stored_key}")
        self.assertEqual(s3_upload["ContentType"], "image/jpeg")
        with Image.open(io.BytesIO(s3_upload["Body"])) as result:
            self.assertEqual(result.format, "JPEG")
        self.assertEqual(
            cursor.execute.call_args_list[-1].args[1][1:],
            ["cat, black cat", "test-user"],
        )

    def test_rerun_skips_successful_imports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images_dir, manifest = self._create_batch(Path(temp_dir))
            first_cursor = MagicMock()
            first_cursor.fetchone.return_value = (1,)
            first_s3 = MagicMock()
            self._run_command(images_dir, manifest, first_cursor, first_s3)

            second_cursor = MagicMock()
            second_cursor.fetchone.return_value = (1,)
            second_s3 = MagicMock()
            self._run_command(images_dir, manifest, second_cursor, second_s3)

        self.assertEqual(first_s3.put_object.call_count, 1)
        second_s3.put_object.assert_not_called()
        second_cursor.execute.assert_called_once()

    def test_database_failure_removes_uploaded_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images_dir, manifest = self._create_batch(Path(temp_dir))
            cursor = MagicMock()
            cursor.fetchone.return_value = (1,)
            cursor.execute.side_effect = [None, DatabaseError("insert failed")]
            s3 = MagicMock()

            with self.assertRaises(CommandError):
                self._run_command(images_dir, manifest, cursor, s3)

        stored_key = cursor.execute.call_args_list[-1].args[1][0]
        s3.delete_object.assert_called_once_with(
            Bucket="test-bucket",
            Key=f"images/{stored_key}",
        )

    def test_import_command_can_leave_user_unlinked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images_dir, manifest = self._create_batch(Path(temp_dir))
            cursor = MagicMock()
            s3 = MagicMock()

            self._run_command(images_dir, manifest, cursor, s3, username=None)
            retry_cursor = MagicMock()
            retry_s3 = MagicMock()
            self._run_command(
                images_dir, manifest, retry_cursor, retry_s3, username=None
            )

        insert_parameters = cursor.execute.call_args.args[1]
        self.assertRegex(insert_parameters[0], r"^[0-9a-f]{32}\.jpg$")
        self.assertEqual(
            insert_parameters,
            [insert_parameters[0], "cat, black cat", None],
        )
        s3.put_object.assert_called_once()
        retry_s3.put_object.assert_not_called()
        retry_cursor.execute.assert_not_called()

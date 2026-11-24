import io
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase
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

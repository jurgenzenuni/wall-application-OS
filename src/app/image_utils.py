import io

from PIL import Image, ImageOps


def convert_image_to_jpeg(source_file):
    with Image.open(source_file) as source_image:
        image = ImageOps.exif_transpose(source_image)
        if "A" in image.getbands() or "transparency" in image.info:
            rgba_image = image.convert("RGBA")
            jpeg_image = Image.new("RGB", rgba_image.size, "white")
            jpeg_image.paste(rgba_image, mask=rgba_image.getchannel("A"))
        else:
            jpeg_image = image.convert("RGB")

        jpeg_content = io.BytesIO()
        jpeg_image.save(jpeg_content, format="JPEG", quality=95, optimize=True)
        return jpeg_content.getvalue()

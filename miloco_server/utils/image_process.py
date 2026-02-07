# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Image processing utilities for miloco_server.
Provides image compression and resizing for vision model optimization.
Ported from miloco_ai_engine/utils/image_process.py
"""

import logging
from io import BytesIO
from typing import Tuple

from PIL import Image

logger = logging.getLogger(__name__)


# Image processing constants (same as miloco_ai_engine)
HIGH_PROCESS_IMAGE_SIZE = (448, 448)
LOW_PROCESS_IMAGE_SIZE = (224, 224)
VIDEO_CONTINUOUS_FRAMES_NUM = 6


class ImageProcess:
    """Image processing utilities for vision model optimization."""

    @staticmethod
    def resize_low_precision(
        image_data: bytes,
        target_size: Tuple[int, int],
        fmt: str = "JPEG",
        quality: int = 60,
        colors: int = 128,
    ) -> bytes:
        """
        Compress and resize input image bytes to specified dimensions with low precision.

        Parameters:
        - image_data: Input raw image bytes
        - target_size: (width, height) target dimensions in pixels
        - fmt: Output format, default JPEG, options: JPEG/PNG/WEBP
        - quality: Lossy format quality (1-95), lower value means higher compression
        - colors: Color palette size limit for lossless formats like PNG (typical: 64/128/256)
        """
        width, height = target_size
        with BytesIO(image_data) as bio:
            with Image.open(bio) as img:
                # Correct image orientation
                try:
                    img = Image.Image.transpose(img, Image.Transpose.EXIF)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

                # Resize (LANCZOS high-quality downsampling)
                img = img.convert("RGBA") if img.mode == "P" else img
                resized = img.resize((width, height), Image.Resampling.LANCZOS)

                out = BytesIO()
                fmt_upper = fmt.upper()

                if fmt_upper == "JPEG":
                    # JPEG requires three channels
                    rgb = resized.convert("RGB")
                    rgb.save(
                        out,
                        format="JPEG",
                        quality=max(1, min(95, quality)),
                        optimize=True,
                        progressive=True,
                        subsampling="4:2:0",
                    )
                elif fmt_upper == "PNG":
                    # Quantize PNG to palette to reduce size
                    paletted = resized.convert("P",
                                               palette=Image.Palette.ADAPTIVE,
                                               colors=max(2, min(256, colors)))
                    paletted.save(out, format="PNG", optimize=True)
                elif fmt_upper == "WEBP":
                    # WebP supports both lossy and lossless, use lossy for smaller size
                    webp_src = resized.convert("RGB")
                    webp_src.save(
                        out,
                        format="WEBP",
                        quality=max(1, min(95, quality)),
                        method=6,
                        exact=False,
                    )
                else:
                    # Fallback to JPEG
                    rgb = resized.convert("RGB")
                    rgb.save(
                        out,
                        format="JPEG",
                        quality=max(1, min(95, quality)),
                        optimize=True,
                        progressive=True,
                        subsampling="4:2:0",
                    )

                return out.getvalue()

    @staticmethod
    def center_crop_to_size(
        image_data: bytes,
        target_size: Tuple[int, int],
        fmt: str = "JPEG",
        quality: int = 85,
        colors: int = 128,
    ) -> bytes:
        """
        Center crop to target aspect ratio, then resize to fixed dimensions and output bytes.

        - First crop from center to maximum content area matching target aspect ratio
        - Then use LANCZOS to resize to exact target dimensions
        - Save in specified format (JPEG/PNG/WEBP), parameters consistent with resize_low_precision
        """
        target_width, target_height = target_size
        target_ratio = target_width / float(target_height)

        with BytesIO(image_data) as bio:
            with Image.open(bio) as img:
                # Correct orientation
                try:
                    img = Image.Image.transpose(img, Image.Transpose.EXIF)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

                src_width, src_height = img.width, img.height
                if src_width == 0 or src_height == 0:
                    raise ValueError("Invalid image size: width/height is zero")

                src_ratio = src_width / float(src_height)

                # Calculate center crop box
                if src_ratio > target_ratio:
                    # Image is wider: align by height, crop left and right
                    new_width = int(round(src_height * target_ratio))
                    new_height = src_height
                else:
                    # Image is taller or equal: align by width, crop top and bottom
                    new_width = src_width
                    new_height = int(round(src_width / target_ratio))

                left = int(round((src_width - new_width) / 2))
                top = int(round((src_height - new_height) / 2))
                right = left + new_width
                bottom = top + new_height

                cropped = img.crop((left, top, right, bottom))

                # Resize to fixed dimensions
                resized = cropped.resize((target_width, target_height), Image.Resampling.LANCZOS)

                out = BytesIO()
                fmt_upper = fmt.upper()

                if fmt_upper == "JPEG":
                    rgb = resized.convert("RGB")
                    rgb.save(
                        out,
                        format="JPEG",
                        quality=max(1, min(95, quality)),
                        optimize=True,
                        progressive=True,
                        subsampling="4:2:0",
                    )
                elif fmt_upper == "PNG":
                    paletted = resized.convert(
                        "P", palette=Image.Palette.ADAPTIVE, colors=max(2, min(256, colors))
                    )
                    paletted.save(out, format="PNG", optimize=True)
                elif fmt_upper == "WEBP":
                    webp_src = resized.convert("RGB")
                    webp_src.save(
                        out,
                        format="WEBP",
                        quality=max(1, min(95, quality)),
                        method=6,
                        exact=False,
                    )
                else:
                    rgb = resized.convert("RGB")
                    rgb.save(
                        out,
                        format="JPEG",
                        quality=max(1, min(95, quality)),
                        optimize=True,
                        progressive=True,
                        subsampling="4:2:0",
                    )

                return out.getvalue()


def process_images_for_vision_model(
    image_list: list[bytes],
    high_precision_size: Tuple[int, int] = HIGH_PROCESS_IMAGE_SIZE,
    low_precision_size: Tuple[int, int] = LOW_PROCESS_IMAGE_SIZE,
    video_frames_num: int = VIDEO_CONTINUOUS_FRAMES_NUM,
) -> list[bytes]:
    """
    Process a list of images for vision model consumption.
    
    This function implements the same logic as miloco_ai_engine:
    - All images are center-cropped to high precision size (448x448)
    - For video sequences, middle frames are compressed to low precision (224x224)
    
    Args:
        image_list: List of raw image bytes
        high_precision_size: Target size for high precision images (default 448x448)
        low_precision_size: Target size for low precision images (default 224x224)
        video_frames_num: Number of frames in a video segment (default 6)
        
    Returns:
        List of processed image bytes
    """
    processed_images = []
    
    for idx, image_data in enumerate(image_list):
        try:
            # First, center crop to high precision size
            processed = ImageProcess.center_crop_to_size(image_data, high_precision_size)
            
            # For video sequences, compress middle frames to low precision
            # Keep first and last frame of each segment at high precision
            if len(image_list) > 1:  # Only apply video logic when multiple images
                if (idx % video_frames_num != 0 and 
                        idx % video_frames_num != video_frames_num - 1):
                    processed = ImageProcess.resize_low_precision(processed, low_precision_size)
            
            processed_images.append(processed)
            
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to process image %d: %s, using original", idx, e)
            processed_images.append(image_data)
    
    return processed_images

import numpy as np
import math
import torchvision.transforms as transforms
from PIL.Image import Image
from PIL.Image import fromarray as ImageFromArray
from torch.nn.functional import interpolate, pad
import torch
from typing import Tuple, Dict

def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.PILToTensor()])  # bgr image
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.float().unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.permute(1, 2, 0).detach().numpy() * 255).astype(np.uint8)

    if np_out.shape[2] == 1:
        np_out = np_out.squeeze(2)
    return ImageFromArray(np_out)

def resize_pad(image: torch.Tensor, dst_size: Tuple[int, int], pad_mode: str = "constant"):
    """
    Resize and pad image to be shape [..., dst_size[0], dst_size[1]]

    Parameters:
        image: (..., H, W)
            Image to reshape.

        dst_size: (height, width)
            Size to which the image should be reshaped.

    Returns:
        rescaled_padded_image: torch.Tensor (..., dst_size[0], dst_size[1])
        scale: scale factor between original image and dst_size image, (w, h)
        pad: pixels of padding added to the rescaled image: (left_padding, top_padding)

    Based on https://github.com/zmurez/MediaPipePyTorch/blob/master/blazebase.py
    """
    height, width = image.shape[-2:]
    dst_frame_height, dst_frame_width = dst_size

    h_ratio = dst_frame_height / height
    w_ratio = dst_frame_width / width
    scale = min(h_ratio, w_ratio)
    if h_ratio < w_ratio:
        scale = h_ratio
        new_height = dst_frame_height
        new_width = math.floor(width * scale)
    else:
        scale = w_ratio
        new_height = math.floor(height * scale)
        new_width = dst_frame_width

    new_height = math.floor(height * scale)
    new_width = math.floor(width * scale)
    pad_h = dst_frame_height - new_height
    pad_w = dst_frame_width - new_width

    pad_top = int(pad_h // 2)
    pad_bottom = int(pad_h // 2 + pad_h % 2)
    pad_left = int(pad_w // 2)
    pad_right = int(pad_w // 2 + pad_w % 2)

    rescaled_image = interpolate(
        image, size=[int(new_height), int(new_width)], mode="bilinear"
    )
    rescaled_padded_image = pad(
        rescaled_image, (pad_left, pad_right, pad_top, pad_bottom), mode=pad_mode
    )
    padding = (pad_left, pad_top)

    return rescaled_padded_image, scale, padding

def undo_resize_pad(
    image: torch.Tensor,
    orig_size_wh: Tuple[int, int],
    scale: float,
    padding: Tuple[int, int],
):
    """
    Undos the efffect of resize_pad. Instead of scale, the original size
    (in order width, height) is provided to prevent an off-by-one size.
    """
    width, height = orig_size_wh

    rescaled_image = interpolate(image, scale_factor=1 / scale, mode="bilinear")

    scaled_padding = [int(round(padding[0] / scale)), int(round(padding[1] / scale))]

    cropped_image = rescaled_image[
        ...,
        scaled_padding[1] : scaled_padding[1] + height,
        scaled_padding[0] : scaled_padding[0] + width,
    ]

    return cropped_image

def pil_resize_pad(
    image: Image, dst_size: Tuple[int, int], pad_mode: str = "constant"
) -> Tuple[Image, float, Tuple[int, int]]:
    torch_image = preprocess_PIL_image(image)
    torch_out_image, scale, padding = resize_pad(
        torch_image,
        dst_size,
        pad_mode=pad_mode
    )
    pil_out_image = torch_tensor_to_PIL_image(torch_out_image[0])
    return (pil_out_image, scale, padding)

def pil_undo_resize_pad(
    image: Image, orig_size_wh: Tuple[int, int], scale: float, padding: Tuple[int, int]
) -> Image:
    torch_image = preprocess_PIL_image(image)
    torch_out_image = undo_resize_pad(torch_image, orig_size_wh, scale, padding)
    pil_out_image = torch_tensor_to_PIL_image(torch_out_image[0])
    return pil_out_image

def preprocess_inputs(
    pixel_values_or_image: Image,
    mask_pixel_values_or_image: Image,
) -> Dict[str, torch.Tensor]:

    NCHW_fp32_torch_frames = preprocess_PIL_image(pixel_values_or_image)
    NCHW_fp32_torch_masks = preprocess_PIL_image(mask_pixel_values_or_image)
    
    # The number of input images should equal the number of input masks.
    if NCHW_fp32_torch_masks.shape[0] != 1:
        NCHW_fp32_torch_masks = NCHW_fp32_torch_masks.tile(
            (NCHW_fp32_torch_frames.shape[0], 1, 1, 1)
        )
  
    # Mask input image
    image_masked = (
        NCHW_fp32_torch_frames * (1 - NCHW_fp32_torch_masks) + NCHW_fp32_torch_masks
    )
    
    return {"image": image_masked, "mask": NCHW_fp32_torch_masks}

def app_to_net_image_inputs(
    pixel_values_or_image: Image,
    to_float: True,
#) -> tuple[list[npt.NDArray[np.uint8]], torch.Tensor]:
):
    """
    Convert the provided images to application inputs.
    ~~This does not change channel order. RGB stays RGB, BGR stays BGR, etc~~

    Parameters:
        pixel_values_or_image: torch.Tensor
            PIL image
            or
            list of PIL images
            or
            numpy array (H W C x uint8) or (N H W C x uint8) -- both BGR or grayscale channel layout
            or
            pyTorch tensor (N C H W x fp32, value range is [0, 1]), BGR or grayscale channel layout

        to_float: bool (default=True)
            Whether to denormalize images to [0,1] (fp32) or keep as uint8.


    Returns:
        NHWC_int_numpy_frames: list[numpy.ndarray]
            List of numpy arrays (one per input image with uint8 dtype, [H W C] shape, and BGR or grayscale layout.
            This output is typically used for use of drawing/displaying images with PIL and CV2

        NCHW_fp32_torch_frames: torch.Tensor
            Tensor of images in fp32 (range 0:1), with shape [Batch, Channels, Height, Width], and BGR or grayscale layout.

    Based on https://github.com/zmurez/MediaPipePyTorch/blob/master/blazebase.py
    """
    NHWC_int_numpy_frames: list[np.ndarray] = []
    NCHW_fp32_torch_frames: torch.Tensor
    if isinstance(pixel_values_or_image, Image):
        pixel_values_or_image = [pixel_values_or_image]
    if isinstance(pixel_values_or_image, list):
        fp32_frames = []
        for image in pixel_values_or_image:
            NHWC_int_numpy_frames.append(np.array(image.convert("RGB")))
            fp32_frames.append(preprocess_PIL_image(image))
        NCHW_fp32_torch_frames = torch.cat(fp32_frames)
    elif isinstance(pixel_values_or_image, torch.Tensor):
        NCHW_fp32_torch_frames = pixel_values_or_image
        for b_img in pixel_values_or_image:
            NHWC_int_numpy_frames.append((b_img.permute(1, 2, 0) * 255).byte().numpy())
    else:
        assert isinstance(pixel_values_or_image, np.ndarray)
        NHWC_int_numpy_frames = (
            [pixel_values_or_image]
            if len(pixel_values_or_image.shape) == 3
            else [x for x in pixel_values_or_image]
        )
        NCHW_fp32_torch_frames = numpy_image_to_torch(pixel_values_or_image)

    return NHWC_int_numpy_frames, NCHW_fp32_torch_frames
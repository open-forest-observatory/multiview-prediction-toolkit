import json
import typing
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from imageio import imread, imwrite

from geograypher.constants import (
    NULL_TEXTURE_INT_VALUE,
    PATH_TYPE,
    TEN_CLASS_VIS_KWARGS,
)
from geograypher.utils.files import ensure_folder


def get_vis_options_from_IDs_to_labels(
    IDs_to_labels: typing.Union[None, dict],
    cmap_continous: str = "viridis",
    cmap_10_classes: str = "tab10",
    cmap_20_classes: str = "tab20",
    cmap_many_classes: str = "viridis",
):
    """Determine vis options based on a given IDs_to_labels object

    Args:
        IDs_to_labels (typing.Union[None, dict]): _description_
        cmap_continous (str, optional):
            Colormap to use if the values are continous. Defaults to "viridis".
        cmap_10_classes (str, optional):
            Colormap to use if the values are discrete and there are 10 or fewer classes. Defaults to "tab10".
        cmap_20_classes (str, optional):
            Colormap to use if the values are discrete and there are 11-20 classes. Defaults to "tab20".
        cmap_many_classes (str, optional):
            Colormap to use if there are more than 20 classes. Defaults to "viridis".

    Returns:
        dict: Containing the cmap, vmin/vmax, and whether the colormap is discrete
    """
    # This could be written in fewer lines of code but I kept it intentionally explicit

    if IDs_to_labels is None:
        # No IDs_to_labels means it's continous
        cmap = cmap_continous
        vmin = None
        vmax = None
        discrete = False
    else:
        # Otherwise, we can determine the max class ID
        max_ID = np.max(list(IDs_to_labels.keys()))

        if max_ID < 10:
            # 10 or fewer discrete classes
            cmap = cmap_10_classes
            vmin = -0.5
            vmax = 9.5
            discrete = True
        elif max_ID < 20:
            # 11-20 discrete classes
            cmap = cmap_20_classes
            vmin = -0.5
            vmax = 19.5
            discrete = True
        else:
            # More than 20 classes. There are no good discrete colormaps for this, so we generally
            # fall back on displaying it with a continous colormap
            cmap = cmap_many_classes
            vmin = None
            vmax = None
            discrete = False

    return {"cmap": cmap, "vmin": vmin, "vmax": vmax, "discrete": discrete}


def create_composite(
    RGB_image: np.ndarray,
    label_image: np.ndarray,
    label_blending_weight: float = 0.5,
    IDs_to_labels: typing.Union[None, dict] = None,
    grayscale_RGB_overlay: bool = True,
):
    """Create a three-panel composite with an RGB image and a label

    Args:
        RGB_image (np.ndarray):
            (h, w, 3) rgb image to be used directly as one panel
        label_image (np.ndarray):
            (h, w) image containing either integer labels or float scalars. Will be colormapped
            prior to display.
        label_blending_weight (float, optional):
            Opacity for the label in the blended composite. Defaults to 0.5.
        IDs_to_labels (typing.Union[None, dict], optional):
            Mapping from integer IDs to string labels. Used to compute colormap. If None, a
            continous colormap is used. Defaults to None.
        grayscale_RGB_overlay (bool):
            Convert the RGB image to grayscale in the overlay. Default is True.

    Raises:
        ValueError: If the RGB image cannot be interpreted as such

    Returns:
        np.ndarray: (h, 3*w, 3) horizontally composited image
    """
    if RGB_image.ndim != 3 or RGB_image.shape[2] != 3:
        raise ValueError("Invalid RGB error")

    if RGB_image.dtype == np.uint8:
        # Rescale to float range and implicitly cast
        RGB_image = RGB_image / 255

    if not (label_image.ndim == 3 and label_image.shape[2] == 3):
        vis_options = get_vis_options_from_IDs_to_labels(IDs_to_labels)
        cmap = plt.get_cmap(vis_options["cmap"])
        null_mask = label_image == NULL_TEXTURE_INT_VALUE
        if not vis_options["discrete"]:
            # Shift
            label_image = label_image - np.nanmin(label_image)
            # Find the max value that's not the null vlaue
            valid_pixels = label_image[np.logical_not(null_mask)]
            if valid_pixels.size > 0:
                # TODO this might have to be changed to nanmax in the future
                max_value = np.max(valid_pixels)
                # Scale
                label_image = label_image / max_value

        # Perform the colormapping
        label_image = cmap(label_image)[..., :3]
        # Mask invalid values
        label_image[null_mask] = 0

    # Create a blended image
    if grayscale_RGB_overlay:
        RGB_for_composite = np.tile(
            np.mean(RGB_image, axis=2, keepdims=True), (1, 1, 3)
        )
    else:
        RGB_for_composite = RGB_image
    overlay = ((1 - label_blending_weight) * RGB_for_composite) + (
        label_blending_weight * label_image
    )
    # Concatenate the images horizonally
    composite = np.concatenate((label_image, RGB_image, overlay), axis=1)
    # Cast to np.uint8 for saving
    composite = (composite * 255).astype(np.uint8)
    return composite


def read_img_npy(filename):
    try:
        return imread(filename)
    except:
        pass

    try:
        return np.load(filename)
    except:
        pass


def show_segmentation_labels(
    label_folder,
    image_folder,
    savefolder: typing.Union[None, PATH_TYPE] = None,
    num_show=10,
    label_suffix=".png",
    image_suffix=".JPG",
    IDs_to_labels=None,
):
    rendered_files = list(Path(label_folder).rglob("*" + label_suffix))
    np.random.shuffle(rendered_files)

    if savefolder is not None:
        ensure_folder(savefolder)

    if (
        IDs_to_labels is None
        and (IDs_to_labels_file := Path(label_folder, "IDs_to_labels.json")).exists()
    ):
        with open(IDs_to_labels_file, "r") as infile:
            IDs_to_labels = json.load(infile)
            IDs_to_labels = {int(k): v for k, v in IDs_to_labels.items()}

    for i, rendered_file in enumerate(rendered_files[:num_show]):
        image_file = Path(
            image_folder, rendered_file.relative_to(label_folder)
        ).with_suffix(image_suffix)

        image = imread(image_file)
        render = read_img_npy(rendered_file)
        composite = create_composite(image, render, IDs_to_labels=IDs_to_labels)

        if savefolder is None:
            plt.imshow(composite)
            plt.show()
        else:
            output_file = Path(savefolder, f"rendered_label_{i:03}.png")
            imwrite(output_file, composite)

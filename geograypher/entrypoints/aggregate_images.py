import argparse
import typing
from pathlib import Path

import numpy as np

from geograypher.cameras import MetashapeCameraSet
from geograypher.constants import (
    EXAMPLE_CAMERAS_FILENAME,
    EXAMPLE_IDS_TO_LABELS,
    EXAMPLE_IMAGE_FOLDER,
    EXAMPLE_MESH_FILENAME,
    EXAMPLE_PREDICTED_LABELS_FOLDER,
    PATH_TYPE,
)
from geograypher.meshes import TexturedPhotogrammetryMesh
from geograypher.segmentation import SegmentorPhotogrammetryCameraSet
from geograypher.segmentation.derived_segmentors import LookUpSegmentor
from geograypher.utils.files import ensure_containing_folder


def aggregate_images(
    mesh_file: PATH_TYPE,
    cameras_file: PATH_TYPE,
    image_folder: PATH_TYPE,
    label_folder: PATH_TYPE,
    subset_images_folder: typing.Union[PATH_TYPE, None] = None,
    mesh_transform_file: typing.Union[PATH_TYPE, None] = None,
    DTM_file: typing.Union[PATH_TYPE, None] = None,
    height_above_ground_threshold: float = 2.0,
    ROI: typing.Union[PATH_TYPE, None] = None,
    ROI_buffer_radius_meters: float = 50,
    IDs_to_labels: typing.Union[dict, None] = None,
    mesh_downsample: float = 1.0,
    n_aggregation_clusters: typing.Union[int, None] = None,
    aggregate_image_scale: float = 1.0,
    aggregated_face_values_savefile: typing.Union[PATH_TYPE, None] = None,
    predicted_face_classes_savefile: typing.Union[PATH_TYPE, None] = None,
    top_down_vector_projection_savefile: typing.Union[PATH_TYPE, None] = None,
    vis: bool = False,
):
    """Aggregate labels from multiple viewpoints onto the surface of the mesh

    Args:
        mesh_file (PATH_TYPE):
            Path to the Metashape-exported mesh file
        cameras_file (PATH_TYPE):
            Path to the MetaShape-exported .xml cameras file
        image_folder (PATH_TYPE):
            Path to the folder of images used to create the mesh
        label_folder (PATH_TYPE):
            Path to the folder of labels to be aggregated onto the mesh. Must be in the same
            structure as the images
        subset_images_folder (typing.Union[PATH_TYPE, None], optional):
            Use only images from this subset. Defaults to None.
        mesh_transform_file (typing.Union[PATH_TYPE, None], optional):
            Transform from the mesh coordinates to the earth-centered, earth-fixed frame. Can be a
            4x4 matrix represented as a .csv, or a Metashape cameras file containing the
            information. Defaults to None.
        DTM_file (typing.Union[PATH_TYPE, None], optional):
            Path to a digital terrain model file to remove ground points. Defaults to None.
        height_above_ground_threshold (float, optional):
            Height in meters above the DTM to consider ground. Only used if DTM_file is set.
            Defaults to 2.0.
        ROI (typing.Union[PATH_TYPE, None], optional):
            Geofile region of interest to crop the mesh to. Defaults to None.
        ROI_buffer_radius_meters (float, optional):
            Keep points within this distance of the provided ROI object, if unset, everything will
            be kept. Defaults to 50.
        IDs_to_labels (typing.Union[dict, None], optional):
            Maps from integer IDs to human-readable class name labels. Defaults to None.
        mesh_downsample (float, optional):
            Downsample the mesh to this fraction of vertices for increased performance but lower
            quality. Defaults to 1.0.
        n_aggregation_clusters (typing.Union[int, None]):
            If set, aggregate with this many clusters. Defaults to None.
        aggregate_image_scale (float, optional):
            Downsample the labels before aggregation for faster runtime but lower quality. Defaults
            to 1.0.
        aggregated_face_values_savefile (typing.Union[PATH_TYPE, None], optional):
            Where to save the aggregated image values as a numpy array. Defaults to None.
        predicted_face_classes_savefile (typing.Union[PATH_TYPE, None], optional):
            Where to save the most common label per face texture as a numpy array. Defaults to None.
        top_down_vector_projection_savefile (typing.Union[PATH_TYPE, None], optional):
            Where to export the predicted map. Defaults to None.
        vis (bool, optional):
            Show the mesh model and predicted results. Defaults to False.
    """
    ## Create the camera set
    # Do the camera operations first because they are fast and good initial error checking
    camera_set = MetashapeCameraSet(cameras_file, image_folder, validate_images=True)

    # If the ROI is not None, subset to cameras within a buffer distance of the ROI
    # TODO let get_subset_ROI accept a None ROI and return the full camera set
    if subset_images_folder is not None:
        camera_set = camera_set.get_cameras_in_folder(subset_images_folder)

    if ROI is not None and ROI_buffer_radius_meters is not None:
        # Extract cameras near the training data
        camera_set = camera_set.get_subset_ROI(
            ROI=ROI, buffer_radius_meters=ROI_buffer_radius_meters
        )

    if mesh_transform_file is None:
        mesh_transform_file = cameras_file

    ## Create the mesh
    mesh = TexturedPhotogrammetryMesh(
        mesh_file,
        transform_filename=mesh_transform_file,
        ROI=ROI,
        ROI_buffer_meters=ROI_buffer_radius_meters,
        IDs_to_labels=IDs_to_labels,
        downsample_target=mesh_downsample,
    )

    # Show the mesh if requested
    if vis:
        mesh.vis(camera_set=camera_set)

    # Create a segmentor object to load in the predictions
    segmentor = LookUpSegmentor(
        base_folder=image_folder,
        lookup_folder=label_folder,
        num_classes=np.max(list(mesh.get_IDs_to_labels().keys())) + 1,
    )
    # Create a camera set that returns the segmented images instead of the original ones
    segmentor_camera_set = SegmentorPhotogrammetryCameraSet(
        camera_set, segmentor=segmentor
    )

    ## Perform aggregation
    # this is the slow step
    if n_aggregation_clusters is None:
        # Aggregate full mesh at once
        aggregated_face_labels, _, _ = mesh.aggregate_viewpoints_pytorch3d(
            segmentor_camera_set,
            image_scale=aggregate_image_scale,
        )
    else:
        # TODO consider whether buffer distance should be tunable. This is fairly conservative
        # but won't neccisarily capture everything
        aggregated_face_labels, _, _ = mesh.aggregate_viewpoints_pytorch3d_by_cluster(
            segmentor_camera_set,
            image_scale=aggregate_image_scale,
            buffer_dist_meters=100,
            n_clusters=n_aggregation_clusters,
            vis_clusters=False,
        )
    # If requested, save this data
    if aggregated_face_values_savefile is not None:
        ensure_containing_folder(aggregated_face_values_savefile)
        np.save(aggregated_face_values_savefile, aggregated_face_labels)

    # Find the most common class per face
    predicted_face_classes = np.argmax(
        aggregated_face_labels, axis=1, keepdims=True
    ).astype(float)

    # If requested, label the ground faces
    if DTM_file is not None and height_above_ground_threshold is not None:
        predicted_face_classes = mesh.label_ground_class(
            labels=predicted_face_classes,
            height_above_ground_threshold=height_above_ground_threshold,
            DTM_file=DTM_file,
            ground_ID=np.nan,
            set_mesh_texture=False,
        )

    if predicted_face_classes_savefile is not None:
        ensure_containing_folder(predicted_face_classes_savefile)
        np.save(predicted_face_classes_savefile, predicted_face_classes)

    if vis:
        # Show the mesh with predicted classes
        mesh.vis(vis_scalars=predicted_face_classes)

    # TODO this should be updated to take IDs_to_labels
    mesh.export_face_labels_vector(
        face_labels=np.squeeze(predicted_face_classes),
        export_file=top_down_vector_projection_savefile,
        vis=True,
    )


def parse_args():
    description = (
        "This script aggregates predictions from individual images onto the mesh. This aggregated "
        + "prediction can then be exported into geospatial coordinates. The default option is to "
        + "use the provided example data. All of the arguments are passed to "
        + "geograypher.entrypoints.workflow_functions.aggregate_images "
        + "which has the following documentation:\n\n"
        + aggregate_images.__doc__
    )
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=description
    )
    parser.add_argument(
        "--mesh-file",
        default=EXAMPLE_MESH_FILENAME,
    )
    parser.add_argument(
        "--cameras-file",
        default=EXAMPLE_CAMERAS_FILENAME,
    )
    parser.add_argument(
        "--image-folder",
        default=EXAMPLE_IMAGE_FOLDER,
    )
    parser.add_argument(
        "--label-folder",
        default=EXAMPLE_PREDICTED_LABELS_FOLDER,
    )
    parser.add_argument(
        "--subset-images-folder",
    )
    parser.add_argument(
        "--mesh-transform-file",
    )
    parser.add_argument(
        "--DTM-file",
    )
    parser.add_argument(
        "--height-above-ground-threshold",
        type=float,
        default=2,
    )
    parser.add_argument("--ROI")
    parser.add_argument(
        "--ROI-buffer-radius-meters",
        default=50,
        type=float,
    )
    parser.add_argument(
        "--IDs-to-labels",
        default=EXAMPLE_IDS_TO_LABELS,
        type=dict,
    )
    parser.add_argument(
        "--mesh-downsample",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--aggregate-image-scale",
        type=float,
        default=0.25,
    )
    parser.add_argument(
        "--aggregated-face-values-savefile",
        type=Path,
    )
    parser.add_argument(
        "--predicted-face-classes-savefile",
        type=Path,
    )
    parser.add_argument(
        "--top-down-vector-projection-savefile",
        default="vis/predicted_map.geojson",
    )
    parser.add_argument("--vis", action="store_true")

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    # Parse command line args
    args = parse_args()
    # Pass command line args to aggregate_images
    aggregate_images(**args.__dict__)

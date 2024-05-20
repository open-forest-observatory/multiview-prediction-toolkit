import typing

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pyproj
from shapely import Point
from sklearn.cluster import KMeans
from tqdm import tqdm

from geograypher.cameras import PhotogrammetryCamera, PhotogrammetryCameraSet
from geograypher.constants import CACHE_FOLDER, PATH_TYPE
from geograypher.meshes import TexturedPhotogrammetryMesh
from geograypher.utils.geospatial import ensure_geometric_CRS


class TexturedPhotogrammetryMeshChunked(TexturedPhotogrammetryMesh):
    """Extends the TexturedPhotogrammtery mesh by allowing chunked operations for large meshes"""

    def get_mesh_chunks_for_cameras(
        self,
        cameras: typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet],
        n_clusters: int = 8,
        buffer_dist_meters=50,
        vis_clusters: bool = False,
        include_texture: bool = False,
    ):
        """Return a generator of sub-meshes, chunked to align with clusters of cameras

        Args:
            cameras (typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet]):
                The chunks of the mesh are generated by clustering the cameras
            n_clusters (int, optional):
                The mesh is broken up into this many clusters. Defaults to 8.
            buffer_dist_meters (int, optional):
                Each cluster contains the mesh that is within this distance in meters of the camera
                locations. Defaults to 50.
            vis_clusters (bool, optional):
                Should the location of the cameras and resultant clusters be shown. Defaults to False.
            include_texture (bool, optional): Should the texture from the full mesh be included
                in the subset mesh. Defaults to False.

        Yields:
            pv.PolyData: The subset mesh
            PhotogrammetryCameraSet: The cameras associated with that mesh
            np.ndarray: The IDs of the faces in the original mesh used to generate the sub mesh

        """
        # Extract the points depending on whether it's a single camera or a set
        if isinstance(cameras, PhotogrammetryCamera):
            camera_points = [Point(*cameras.get_lon_lat())]
        else:
            # Get the lat lon for each camera point and turn into a shapely Point
            camera_points = [
                Point(*lon_lat) for lon_lat in cameras.get_lon_lat_coords()
            ]

        # Create a geodataframe from the points
        camera_points = gpd.GeoDataFrame(
            geometry=camera_points, crs=pyproj.CRS.from_epsg("4326")
        )
        # Make sure the gdf has a gemetric CRS so there is no warping of the space
        camera_points = ensure_geometric_CRS(camera_points)
        # Extract the x, y points now in a geometric CRS
        camera_points_numpy = np.stack(
            camera_points.geometry.apply(lambda point: (point.x, point.y))
        )

        # Assign each camera to a cluster
        camera_cluster_IDs = KMeans(n_clusters=n_clusters).fit_predict(
            camera_points_numpy
        )
        if vis_clusters:
            # Show the camera locations, colored by which one they were assigned to
            plt.scatter(
                camera_points_numpy[:, 0],
                camera_points_numpy[:, 1],
                c=camera_cluster_IDs,
                cmap="tab20",
            )
            plt.show()

        # Get the texture from the full mesh
        full_mesh_texture = (
            self.get_texture(request_vertex_texture=False) if include_texture else None
        )

        # Iterate over the clusters of cameras
        for cluster_ID in tqdm(range(n_clusters), desc="Chunks in mesh"):
            # Get indices of cameras for that cluster
            matching_camera_inds = np.where(cluster_ID == camera_cluster_IDs)[0]
            # Get the segmentor camera set for the subset of the camera inds
            sub_camera_set = cameras.get_subset_cameras(matching_camera_inds)
            # Extract the rows in the dataframe for those IDs
            subset_camera_points = camera_points.iloc[matching_camera_inds]

            # TODO this could be accellerated by computing the membership for all points at the begining.
            # This would require computing all the ROIs (potentially-overlapping) for each region first. Then, finding all the non-overlapping
            # partition where each polygon corresponds to a set of ROIs. Then the membership for each vertex could be found for each polygon
            # and the membership in each ROI could be computed. This should be benchmarked though, because having more polygons than original
            # ROIs may actually lead to slower computations than doing it sequentially

            # Extract a sub mesh for a region around the camera points and also retain the indices into the original mesh
            sub_mesh_pv, _, face_IDs = self.select_mesh_ROI(
                region_of_interest=subset_camera_points,
                buffer_meters=buffer_dist_meters,
                return_original_IDs=True,
            )
            # Extract the corresponding texture elements for this sub mesh if needed
            # If include_texture=False, the full_mesh_texture will not be set
            # If there is no mesh, the texture should also be set to None, otherwise it will be
            # ambigious whether it's a face or vertex texture
            sub_mesh_texture = (
                full_mesh_texture[face_IDs]
                if full_mesh_texture is not None and len(face_IDs) > 0
                else None
            )

            # Wrap this pyvista mesh in a photogrammetry mesh
            sub_mesh_TPM = TexturedPhotogrammetryMesh(
                sub_mesh_pv, texture=sub_mesh_texture
            )

            # Return the submesh as a Textured Photogrammetry Mesh, the subset of cameras, and the
            # face IDs mapping the faces in the sub mesh back to the full one
            yield sub_mesh_TPM, sub_camera_set, face_IDs

    def render_flat(
        self,
        cameras: typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet],
        batch_size: int = 1,
        render_img_scale: float = 1,
        n_clusters: int = 8,
        buffer_dist_meters: float = 50,
        vis_clusters: bool = False,
        **pix2face_kwargs
    ):
        """
        Render the texture from the viewpoint of each camera in cameras. Note that this is a
        generator so if you want to actually execute the computation, call list(*) on the output.
        This version first clusters the cameras, extracts a region of the mesh surrounding each
        cluster of cameras, and then performs rendering on each sub-region.

        Args:
            cameras (typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet]):
                Either a single camera or a camera set. The texture will be rendered from the
                perspective of each one
            batch_size (int, optional):
                The batch size for pix2face. Defaults to 1.
            render_img_scale (float, optional):
                The rendered image will be this fraction of the original image corresponding to the
                virtual camera. Defaults to 1.
            n_clusters (int, optional):
                Number of clusters to break the cameras into. Defaults to 8.
            buffer_dist_meters (float, optional):
                How far around the cameras to include the mesh. Defaults to 50.
            vis_clusters (bool, optional):
                Should the clusters of camera locations be shown. Defaults to False.

        Raises:
            TypeError: If cameras is not the correct type

        Yields:
            np.ndarray:
               The pix2face array for the next camera. The shape is
               (int(img_h*render_img_scale), int(img_w*render_img_scale)).
        """
        # Create a generator to chunked meshes based on clusters of cameras
        chunk_gen = self.get_mesh_chunks_for_cameras(
            cameras,
            n_clusters=n_clusters,
            buffer_dist_meters=buffer_dist_meters,
            vis_clusters=vis_clusters,
            include_texture=True,
        )

        for sub_mesh_TPM, sub_camera_set, _ in tqdm(
            chunk_gen, total=n_clusters, desc="Rendering by chunks"
        ):
            # Create the render generator
            render_gen = sub_mesh_TPM.render_flat(
                sub_camera_set,
                batch_size=batch_size,
                render_img_scale=render_img_scale,
                **pix2face_kwargs
            )
            # Yield items from the returned generator
            for render_item in render_gen:
                yield render_item

    def aggregate_projected_images(
        self,
        cameras: typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet],
        batch_size: int = 1,
        aggregate_img_scale: float = 1,
        n_clusters: int = 8,
        buffer_dist_meters: float = 50,
        vis_clusters: bool = False,
        **kwargs
    ):
        """
        Aggregate the imagery from multiple cameras into per-face averges. This version chunks the
        mesh up and performs aggregation on sub-regions to decrease the runtime.

        Args:
            cameras (typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet]):
                The cameras to aggregate the images from. cam.get_image() will be called on each
                element.
            batch_size (int, optional):
                The number of cameras to compute correspondences for at once. Defaults to 1.
            aggregate_img_scale (float, optional):
                The scale of pixel-to-face correspondences image, as a fraction of the original
                image. Lower values lead to better runtimes but decreased precision at content
                boundaries in the images. Defaults to 1.
            n_clusters (int, optional):
                The mesh is broken up into this many clusters. Defaults to 8.
            buffer_dist_meters (int, optional):
                Each cluster contains the mesh that is within this distance in meters of the camera
                locations. Defaults to 50.
            vis_clusters (bool, optional):
                Should the location of the cameras and resultant clusters be shown. Defaults to False.

        Returns:
            np.ndarray: (n_faces, n_image_channels) The average projected image per face
            dict: Additional information, including the summed projections, observations per face,
                  and potentially each individual projection
        """

        # Initialize the values that will be incremented per cluster
        summed_projections = np.zeros(
            (self.pyvista_mesh.n_faces, cameras.n_image_channels()), dtype=float
        )
        projection_counts = np.zeros(self.pyvista_mesh.n_faces, dtype=int)

        # Create a generator to generate chunked meshes
        chunk_gen = self.get_mesh_chunks_for_cameras(
            cameras,
            n_clusters=n_clusters,
            buffer_dist_meters=buffer_dist_meters,
            vis_clusters=vis_clusters,
        )

        # Iterate over chunks in the mesh
        for sub_mesh_TPM, sub_camera_set, face_IDs in chunk_gen:
            # This means there was no mesh for these cameras
            if len(face_IDs) == 0:
                continue

            # Aggregate the projections from a set of cameras corresponding to
            _, additional_information_submesh = sub_mesh_TPM.aggregate_projected_images(
                sub_camera_set,
                batch_size=batch_size,
                aggregate_img_scale=aggregate_img_scale,
                return_all=False,
                **kwargs
            )

            # Increment the summed predictions and counts
            # Make sure that nans don't propogate, since they should just be treated as zeros
            # TODO ensure this is correct
            summed_projections[face_IDs] = np.nansum(
                [
                    summed_projections[face_IDs],
                    additional_information_submesh["summed_projections"],
                ],
                axis=0,
            )
            projection_counts[face_IDs] = (
                projection_counts[face_IDs]
                + additional_information_submesh["projection_counts"]
            )

        # Same as the parent class
        no_projections = projection_counts == 0
        summed_projections[no_projections] = np.nan

        additional_information = {
            "projection_counts": projection_counts,
            "summed_projections": summed_projections,
        }

        average_projections = np.divide(
            summed_projections, np.expand_dims(projection_counts, 1)
        )

        return average_projections, additional_information
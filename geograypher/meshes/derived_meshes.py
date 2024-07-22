import typing

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pyproj
from scipy.sparse import csr_array
from shapely import Point
from sklearn.cluster import KMeans
from tqdm import tqdm

from geograypher.cameras import PhotogrammetryCamera, PhotogrammetryCameraSet
from geograypher.constants import CACHE_FOLDER, PATH_TYPE
from geograypher.meshes import TexturedPhotogrammetryMesh
from geograypher.utils.geospatial import coerce_to_geoframe, ensure_projected_CRS


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
        camera_points = ensure_projected_CRS(camera_points)
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
        **pix2face_kwargs,
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
                **pix2face_kwargs,
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
        **kwargs,
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
                **kwargs,
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

    def label_polygons(
        self,
        face_labels: np.ndarray,
        polygons: typing.Union[PATH_TYPE, gpd.GeoDataFrame],
        face_weighting: typing.Union[None, np.ndarray] = None,
        sjoin_overlay: bool = True,
        return_class_labels: bool = True,
        unknown_class_label: str = "unknown",
        buffer_dist_meters: float = 2,
        n_polygons_per_cluster: int = 1000,
    ):
        """
        Assign a class label to polygons using labels per face. This implementation is useful for
        large numbers of polygons. To make the expensive sjoin/overlay more efficient, this
        implementation first clusters the polygons and labels each cluster indepenently. This makes
        use of the fact that the mesh faces around this cluster can be extracted relatively quickly.
        Then the sjoin/overlay is computed with substaintially-fewer polygons and faces, leading to
        better performance.

        Args:
            face_labels (np.ndarray): (n_faces,) array of integer labels
            polygons (typing.Union[PATH_TYPE, gpd.GeoDataFrame]): Geospatial polygons to be labeled
            face_weighting (typing.Union[None, np.ndarray], optional):
                (n_faces,) array of scalar weights for each face, to be multiplied with the
                contribution of this face. Defaults to None.
            sjoin_overlay (bool, optional):
                Whether to use `gpd.sjoin` or `gpd.overlay` to compute the overlay. Sjoin is
                substaintially faster, but only uses mesh faces that are entirely within the bounds
                of the polygon, rather than computing the intersecting region for
                partially-overlapping faces. Defaults to True.
            return_class_labels: (bool, optional):
                Return string representation of class labels rather than float. Defaults to True.
            unknown_class_label (str, optional):
                Label for predicted class for polygons with no overlapping faces. Defaults to "unknown".
            buffer_dist_meters: (Union[float, None], optional)
                Only applicable if sjoin_overlay=False. In that case, include faces entirely within
                the region that is this distance in meters from the polygons. Defaults to 2.0.
            n_polygons_per_cluster: (int):
                Set the number of clusters so there are approximately this number polygons per
                cluster on average. Defaults to 1000

        Raises:
            ValueError: if faces_labels or face_weighting is not 1D

        Returns:
            list(typing.Union[str, int]):
                (n_polygons,) list of labels. Either float values, represnting integer IDs or nan,
                or string values representing the class label
        """
        # Load in the polygons
        polygons_gdf = ensure_projected_CRS(coerce_to_geoframe(polygons))
        # Extract the centroid of each one and convert to a numpy array
        centroids_xy = np.stack(
            polygons_gdf.centroid.apply(lambda point: (point.x, point.y))
        )
        # Determine how many clusters there should be
        n_clusters = int(np.ceil(len(polygons_gdf) / n_polygons_per_cluster))
        # Assign each polygon to a cluster
        polygon_cluster_IDs = KMeans(n_clusters=n_clusters).fit_predict(centroids_xy)

        # This will be set later once we figure out the datatype of the per-cluster labels
        all_labels = None

        # Loop over the individual clusters
        for cluster_ID in tqdm(range(n_clusters), desc="Clusters of polygons"):
            # Determine which polygons are part of that cluster
            cluster_mask = polygon_cluster_IDs == cluster_ID
            # Extract the polygons from one cluster
            cluster_polygons = polygons_gdf.iloc[cluster_mask]
            # Compute the labeling per polygon
            cluster_labels = super().label_polygons(
                face_labels,
                cluster_polygons,
                face_weighting,
                sjoin_overlay,
                return_class_labels,
                unknown_class_label,
                buffer_dist_meters,
            )
            # Convert to numpy array
            cluster_labels = np.array(cluster_labels)
            # Create the aggregation array with the appropriate datatype
            if all_labels is None:
                # We assume that this list will be at least one element since each cluster
                # should be non-empty. All values should be overwritten so the default value doesn't matter
                all_labels = np.zeros(len(polygons_gdf), dtype=cluster_labels.dtype)

            # Set the appropriate elements of the full array with the newly-computed cluster labels
            all_labels[cluster_mask] = cluster_labels

        # The output is expected to be a list
        all_labels = all_labels.tolist()
        return all_labels


class TexturedPhotogrammetryMeshIndexPredictions(TexturedPhotogrammetryMesh):
    def aggregate_projected_images(
        self,
        cameras: typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet],
        n_classes: int,
        batch_size: int = 1,
        aggregate_img_scale: float = 1,
        return_all: bool = False,
        **kwargs,
    ) -> typing.Tuple[np.ndarray, dict]:
        """
        Aggregate the imagery from multiple cameras into per-face averges. This implementation uses
        sparse arrays to process data where there are large numbers of discrete classes and an
        explicit one-hot encoding would take up too much space

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
            return_all (bool, optional):
                Return the projection of each individual image, rather than just the aggregates.
                Defaults to False.

        Returns:
            np.ndarray: (n_faces, n_image_channels) The average projected image per face
            dict: Additional information, including the summed projections, observations per face,
                  and potentially each individual projection
        """
        # TODO this should be a convenience method
        n_faces = self.faces.shape[0]

        # Initialize list for all projections if requested
        if return_all:
            all_projections = []

        # Initialize sparse arrays for number of projections per face and the summed projections
        projection_counts = csr_array((n_faces, 1), dtype=np.uint16)
        summed_projections = csr_array((n_faces, n_classes), dtype=np.uint16)

        # Create a generator for all the projections
        project_images_generator = self.project_images(
            cameras=cameras,
            batch_size=batch_size,
            aggregate_img_scale=aggregate_img_scale,
            check_null_image=True,
            **kwargs,
        )

        # Iterate over projections in the generator
        for projection_for_image in tqdm(
            project_images_generator,
            total=len(cameras),
            desc="Aggregating projected viewpoints",
        ):
            # Append the projection for that image
            if return_all:
                all_projections.append(projection_for_image)

            # Determine which pixels in the image have non-null projections
            projected_face_inds = np.nonzero(
                np.isfinite(np.squeeze(projection_for_image))
            )[0]

            # If there's no projected classes, this is just wasted compute and also can cause errors
            # because indexing with an empty array returns all values
            if len(projected_face_inds) == 0:
                continue

            # Create an array for faces which were projected to by this image
            new_projection_counts = csr_array(
                (
                    np.ones_like(projected_face_inds, dtype=np.uint16),
                    (
                        projected_face_inds,
                        np.zeros_like(projected_face_inds, dtype=np.uint16),
                    ),
                ),
                shape=(n_faces, 1),
            )
            # Add this to the running tally variable
            projection_counts = projection_counts + new_projection_counts

            # Determine the classes for each non-null projection
            projected_face_classes = projection_for_image[
                projected_face_inds, 0
            ].astype(int)

            # Find the current value for the summed projection for given face, class pairs
            old_values_for_projected_elements = summed_projections[
                projected_face_inds, projected_face_classes
            ]
            # Increment the previous value by one
            incremented_old_values_for_projected_elements = (
                old_values_for_projected_elements + 1
            )

            # Set the running tally to the updated value
            summed_projections[projected_face_inds, projected_face_classes] = (
                incremented_old_values_for_projected_elements
            )

        # Record the information
        additional_information = {
            "projection_counts": projection_counts,
            "summed_projections": summed_projections,
        }
        if return_all:
            additional_information["all_projections"] = all_projections

        # Perform the normalization by the counts
        # We can't do per-element division of sparse matrices so instead we just take the reciprocal
        # of each count and then multiply it these values by the summed projections
        # https://stackoverflow.com/questions/21080430/taking-reciprocal-of-each-elements-in-a-sparse-matrix
        projection_counts_reciprocal = csr_array(
            (
                (
                    np.reciprocal(projection_counts.data),
                    projection_counts.indices,
                    projection_counts.indptr,
                )
            ),
            shape=projection_counts.shape,
        )
        # Normalize the summed projection by the number of observations for that face
        average_projections = summed_projections.multiply(projection_counts_reciprocal)

        return average_projections, additional_information


class TexturedPhotogrammetryMeshPyTorch3dRendering(TexturedPhotogrammetryMesh):
    """Extends the TexturedPhotogrammtery mesh by rendering using PyTorch3d"""

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        # Import PyTorch3D modules
        try:
            import torch
            from pytorch3d.renderer import (
                MeshRasterizer,
                PerspectiveCameras,
                RasterizationSettings,
                TexturesVertex,
            )
            from pytorch3d.structures import Meshes

            # Assign imported modules to instance variables for later use
            self.torch = torch
            self.TexturesVertex = TexturesVertex
            self.RasterizationSettings = RasterizationSettings
            self.PerspectiveCameras = PerspectiveCameras
            self.MeshRasterizer = MeshRasterizer
            self.Meshes = Meshes
        except ImportError:
            raise ImportError(
                "PyTorch3D is not installed. Please call install PyTorch3D or use the pix2face method from the TexturedPhotogrammetryMesh class."
            )

        if torch.cuda.is_available():
            self.device = torch.device("cuda:0")
            torch.cuda.set_device(self.device)
        else:
            self.device = torch.device("cpu")

    def create_pytorch3d_mesh(
        self,
        vert_texture: np.ndarray = None,
        batch_size: int = 1,
    ):
        """Create the pytorch_3d_mesh

        Args:
            vert_texture (np.ndarray, optional):
                Optional texture, (n_verts, n_channels). In the range [0, 1]. Defaults to None.
            batch_size (int):
                Number of copies of the mesh to create in a batch. Defaults to 1.
        """

        # Create the texture object if provided
        if vert_texture is not None:
            vert_texture = (
                self.torch.Tensor(vert_texture)
                .to(self.torch.float)
                .to(self.device)
                .unsqueeze(0)
            )
            if len(vert_texture.shape) == 2:
                vert_texture = vert_texture.unsqueeze(-1)
            texture = self.TexturesVertex(verts_features=vert_texture).to(self.device)
        else:
            texture = None

        # Create the pytorch mesh
        pytorch3d_mesh = self.Meshes(
            verts=[self.torch.Tensor(self.pyvista_mesh.points).to(self.device)],
            faces=[self.torch.Tensor(self.faces).to(self.device)],
            textures=texture,
        ).to(self.device)

        # Ensure the batch size matches the number of meshes
        if batch_size != len(pytorch3d_mesh):
            pytorch3d_mesh = pytorch3d_mesh.extend(batch_size)

        return pytorch3d_mesh

    def pix2face(
        self,
        cameras: typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet],
        render_img_scale: float = 1,
        save_to_cache: bool = False,
        cache_folder: typing.Union[None, PATH_TYPE] = CACHE_FOLDER,
        cull_to_frustum: bool = False,
    ) -> np.ndarray:
        """Use pytorch3d to get correspondences between pixels and vertices

        Args:
            cameras (typing.Union[PhotogrammetryCamera, PhotogrammetryCameraSet]):
                A single camera or set of cameras. For each camera, the correspondences between
                pixels and the face IDs of the mesh will be computed. The images of all cameras
                are assumed to be the same size.
            render_img_scale (float, optional):
                Create a pix2face map that is this fraction of the original image scale. Defaults
                to 1.
            save_to_cache (bool, optional):
                Should newly-computed values be saved to the cache. This may speed up future operations
                but can take up 100s of GBs of space. Defaults to False.
            cache_folder ((PATH_TYPE, None), optional):
                Where to check for and save to cached data. Only applicable if use_cache=True.
                Defaults to CACHE_FOLDER
            cull_to_frustum (bool, optional):
                If True, enables frustum culling to exclude mesh faces outside the camera's view,
                Defaults to False.

        Returns:
            np.ndarray: For each camera, returns an array of face indices corresponding to each pixel
            in the image. Indices are adjusted for batch offsets and set to -1 where no valid face is
            found. If the input is a single PhotogrammetryCamera, the shape is (h, w). If it's a camera
            set, then it is (n_cameras, h, w).
        """

        # Create a camera from the metashape parameters
        if isinstance(cameras, PhotogrammetryCamera):
            p3d_cameras = self.get_single_pytorch3d_camera(camera=cameras)
            image_size = cameras.get_image_size(image_scale=render_img_scale)
        else:
            p3d_cameras = self.transform_into_pytorch3d_camera_set(cameras=cameras)
            image_size = cameras[0].get_image_size(image_scale=render_img_scale)

        raster_settings = self.RasterizationSettings(
            image_size=image_size,
            blur_radius=0.0,
            faces_per_pixel=1,
            cull_to_frustum=cull_to_frustum,
        )

        # Don't wrap this in a MeshRenderer like normal because we need intermediate results
        rasterizer = self.MeshRasterizer(
            cameras=p3d_cameras, raster_settings=raster_settings
        ).to(self.device)

        # Create a pytorch3d mesh
        pytorch3d_mesh = self.create_pytorch3d_mesh(batch_size=len(p3d_cameras))

        # Perform the expensive pytorch3d operation
        fragments = rasterizer(pytorch3d_mesh)

        # Extract pix_to_face from fragments, move it to the CPU, and convert tensor to NumPy array
        pix_to_face = fragments.pix_to_face.cpu().numpy()

        # Removes last dimension which is number of faces that can corrrespond to pixel
        # pix_to_face now is (batch_size, height, width)
        pix_to_face = pix_to_face[:, :, :, 0]

        # Create an array mask to note where pix_to_face correspondances are -1 (invalid)
        invalid_mask = pix_to_face == -1

        # Track batch index offset to account for mesh extension
        offset_array = np.arange(
            0,
            self.pyvista_mesh.n_faces * pix_to_face.shape[0],
            self.pyvista_mesh.n_faces,
        )

        # Convert dimensions of offset_array from (batch_size,) to (batch_size, 1, 1) in order to match pix_to_face dimensions
        offset_array = np.expand_dims(offset_array, (1, 2))

        # Adjust pix_to_face indices based on the batch offset
        pix_to_face = pix_to_face - offset_array

        # Add -1 (invalid) values back into pix_to_face
        pix_to_face[invalid_mask] = -1

        return pix_to_face

    def get_single_pytorch3d_camera(self, camera: PhotogrammetryCamera):
        """Return a pytorch3d camera based on the parameters from metashape

        Args:
            camera (PhotogrammetryCamera): The camera to be converted into a pythorch3d camera

        Returns:
            pytorch3d.renderer.PerspectiveCameras:
        """

        # Retrieve intrinsic camera properties
        camera_properties = camera.get_camera_properties()

        rotation_about_z = np.array(
            [[-1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        )
        # Rotate about the Z axis because the NDC coordinates are defined X: left, Y: up and we use X: right, Y: down
        # See https://pytorch3d.org/docs/cameras
        transform_4x4_world_to_cam = (
            rotation_about_z @ camera_properties["world_to_cam_transform"]
        )

        R = self.torch.Tensor(
            np.expand_dims(transform_4x4_world_to_cam[:3, :3].T, axis=0)
        )
        T = self.torch.Tensor(np.expand_dims(transform_4x4_world_to_cam[:3, 3], axis=0))

        # The image size is (height, width) which completely disreagards any other conventions they use...
        image_size = (
            (camera_properties["image_height"], camera_properties["image_width"]),
        )
        # These parameters are in screen (pixel) coordinates.
        # TODO see if a normalized version is more robust for any reason
        fcl_screen = (camera_properties["focal_length"],)
        prc_points_screen = (
            (
                camera_properties["image_width"] / 2
                + camera_properties["principal_point_x"],
                camera_properties["image_height"] / 2
                + camera_properties["principal_point_y"],
            ),
        )

        # Create camera
        # TODO use the pytorch3d FishEyeCamera model that uses distortion
        # https://pytorch3d.readthedocs.io/en/latest/modules/renderer/fisheyecameras.html?highlight=distortion
        cameras = self.PerspectiveCameras(
            R=R,
            T=T,
            focal_length=fcl_screen,
            principal_point=prc_points_screen,
            device=self.device,
            in_ndc=False,  # screen coords
            image_size=image_size,
        )
        return cameras

    def transform_into_pytorch3d_camera_set(self, cameras: PhotogrammetryCameraSet):
        """
        Return a pytorch3d cameras object based on the parameters from metashape.
        This has the information from each of the camears in the set to enabled batched rendering.

        Args:
            cameras (PhotogrammetryCameraSet): Set of cameras to be converted into pytorch3d cameras

        Returns:
            pytorch3d.renderer.PerspectiveCameras:
        """
        # Get the pytorch3d cameras for each of the cameras in the set
        p3d_cameras = [
            self.get_single_pytorch3d_camera(self.device, camera) for camera in cameras
        ]
        # Get the image sizes
        image_sizes = [camera.image_size.cpu().numpy() for camera in p3d_cameras]
        # Check that all the image sizes are the same because this is required for proper batched rendering
        if np.any([image_size != image_sizes[0] for image_size in image_sizes]):
            raise ValueError("Not all cameras have the same image size")
        # Create the new pytorch3d cameras object with the information from each camera
        cameras = self.PerspectiveCameras(
            R=self.torch.cat([camera.R for camera in p3d_cameras], 0),
            T=self.torch.cat([camera.T for camera in p3d_cameras], 0),
            focal_length=self.torch.cat(
                [camera.focal_length for camera in p3d_cameras], 0
            ),
            principal_point=self.torch.cat(
                [camera.get_principal_point() for camera in p3d_cameras], 0
            ),
            device=self.device,
            in_ndc=False,  # screen coords
            image_size=image_sizes[0],
        )
        return cameras

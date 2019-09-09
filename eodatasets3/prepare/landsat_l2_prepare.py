"""
Prepare eo3 metadata for USGS Landsat Level 1 data.

Input dataset paths can be directories or tar files.
"""

import logging

import click
import fsspec
import rasterio
import uuid
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Generator, Union

from eodatasets3 import utils, DatasetAssembler, IfExists, images
from eodatasets3.model import FileFormat
from eodatasets3.prepare.utils import read_mtl
from eodatasets3.ui import UrlOrPath
from eodatasets3.utils import SimpleUrl

_COPYABLE_MTL_FIELDS = [
    (
        "level1_processing_record",
        (
            "landsat_scene_id",
            "landsat_product_id",
            "processing_software_version",
            "ground_control_points_version",
            "ground_control_points_model",
            "geometric_rmse_model_x",
            "geometric_rmse_model_y",
            "ground_control_points_verify",
            "geometric_rmse_verify",
        ),
    ),
    ("product_contents", ("collection_category")),
    ("image_attributes", ("station_id", "wrs_path", "wrs_row")),
]

# Static namespace to generate uuids for datacube indexing
USGS_UUID_NAMESPACE = uuid.UUID("276af61d-99f8-4aa3-b2fb-d7df68c5e28f")

BAND_CONFIGURATIONS = {
    "band_1": {
        "output_name": "b1",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "band_2": {
        "output_name": "b2",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "band_3": {
        "output_name": "b3",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "band_4": {
        "output_name": "b4",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "band_5": {
        "output_name": "b5",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "band_6": {
        "output_name": "b6",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "band_7": {
        "output_name": "b7",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "band_st_b10": {
        "output_name": "b10",
        "nodata": 0,
        "dtype": "int16",
        "overviews": images.DEFAULT_OVERVIEWS,
    },
    "thermal_radiance": {
        "output_name": "thermal_radiance",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "upwell_radiance": {
        "output_name": "upwell_radiance",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "downwell_radiance": {
        "output_name": "downwell_radiance",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "atmospheric_transmittance": {
        "output_name": "atmospheric_transmittance",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "emissivity": {
        "output_name": "emissivity",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "emissivity_stdev": {
        "output_name": "emissivity_stdev",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "cloud_distance": {
        "output_name": "cloud_distance",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "quality_l2_aerosol": {
        "output_name": "quality_l2_aerosol",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": (),
    },
    "quality_l2_surface_temperature": {
        "output_name": "quality_l2_surface_temperature",
        "nodata": -9999,
        "dtype": "int16",
        "overviews": (),
    },
    "quality_l1_pixel": {
        "output_name": "quality_l1_pixel",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": (),
    },
    "quality_l1_radiometric_saturation": {
        "output_name": "quality_l1_radiometric_saturation",
        "nodata": 0,
        "dtype": "uint16",
        "overviews": (),
    },
}


def _iter_bands_paths(mtl_doc: Dict) -> Generator[Tuple[str, str], None, None]:
    prefix = "file_name_"
    suffix = "TIF"
    for name, filepath in mtl_doc["product_contents"].items():
        if not name.startswith(prefix):
            continue
        if not filepath.endswith(suffix):
            continue
        usgs_band_id = name[len(prefix) :]
        yield usgs_band_id, filepath


def prepare_and_write(
    ds_path: SimpleUrl,
    collection_location: Union[SimpleUrl, Path],
    # TODO: Can we infer producer automatically? This is bound to cause mistakes othewise
    producer="usgs.gov",
) -> Tuple[uuid.UUID, Path]:
    """
    Prepare an eo3 metadata file for a Level2 dataset.

    """
    with fsspec.open(ds_path, "r") as fp:
        mtl_doc = read_mtl(fp, root_element="landsat_metadata_file")

    if not mtl_doc:
        raise ValueError(f"No MTL file found for {ds_path}")

    usgs_collection_number = mtl_doc["product_contents"].get("collection_number")
    if usgs_collection_number is None:
        raise NotImplementedError(
            "Dataset has no collection number: pre-collection data is not supported."
        )

    data_format = mtl_doc["product_contents"]["output_format"]
    if data_format.upper() != "GEOTIFF":
        raise NotImplementedError(f"Only GTiff currently supported, got {data_format}")
    file_format = FileFormat.GeoTIFF

    # Assumed below.
    if (
        mtl_doc["projection_attributes"]["grid_cell_size_reflective"]
        != mtl_doc["projection_attributes"]["grid_cell_size_thermal"]
    ):
        raise NotImplementedError("reflective and thermal have different cell sizes")
    ground_sample_distance = min(
        value
        for name, value in mtl_doc["projection_attributes"].items()
        if name.startswith("grid_cell_size_")
    )

    with DatasetAssembler(
        collection_location=collection_location,
        # Detministic ID based on USGS's product id (which changes when the scene is reprocessed by them)
        dataset_id=uuid.uuid5(
            USGS_UUID_NAMESPACE, mtl_doc["product_contents"]["landsat_product_id"]
        ),
        naming_conventions="dea",
        if_exists=IfExists.Overwrite,
    ) as p:
        p.platform = mtl_doc["image_attributes"]["spacecraft_id"]
        p.instrument = mtl_doc["image_attributes"]["sensor_id"]
        p.product_family = "level2"
        p.producer = producer
        p.datetime = "{}T{}".format(
            mtl_doc["image_attributes"]["date_acquired"],
            mtl_doc["image_attributes"]["scene_center_time"],
        )
        # p.processed = mtl_doc["metadata_file_info"]["file_date"]
        p.processed = mtl_doc["level2_processing_record"]["date_product_generated"]
        p.properties["odc:file_format"] = file_format
        p.properties["eo:gsd"] = ground_sample_distance
        p.properties["eo:cloud_cover"] = mtl_doc["image_attributes"]["cloud_cover"]
        p.properties["eo:sun_azimuth"] = mtl_doc["image_attributes"]["sun_azimuth"]
        p.properties["eo:sun_elevation"] = mtl_doc["image_attributes"]["sun_elevation"]
        p.properties["landsat:collection_number"] = usgs_collection_number
        for section, fields in _COPYABLE_MTL_FIELDS:
            for field in fields:
                value = mtl_doc[section].get(field)
                if value is not None:
                    p.properties[f"landsat:{field}"] = value

        p.region_code = f"{p.properties['landsat:wrs_path']:03d}{p.properties['landsat:wrs_row']:03d}"
        org_collection_number = utils.get_collection_number(
            p.producer, p.properties["landsat:collection_number"]
        )
        p.dataset_version = f"{org_collection_number}.0.{p.processed:%Y%m%d}"

        p.copy_accessory_file("metadata:landsat_mtl", ds_path)
        bands = _iter_bands_paths(mtl_doc)
        for usgs_band_id, file_location in bands:
            # p.note_measurement(
            #     band_aliases[usgs_band_id],
            #     file_location,
            #     relative_to_dataset_location=True,
            # )
            band_config = BAND_CONFIGURATIONS.get(usgs_band_id)

            if band_config is not None:
                path_file = ds_path.parent / file_location
                p.write_measurement(
                    band_config["output_name"],
                    path_file,
                    overviews=band_config["overviews"],
                )

        return p.done(sort_measurements=False)


@click.command(help=__doc__)
@click.option(
    "--output-base",
    help="Write output into this directory instead of with the dataset",
    required=True,
    type=UrlOrPath(),
)
@click.option(
    "--producer",
    help="Organisation that produced the data: probably either 'ga.gov.au' or 'usgs.gov'.",
    required=False,
    default="usgs.gov",
)
@click.argument("datasets", type=UrlOrPath(), nargs=-1)
def main(
    output_base: Optional[Union[SimpleUrl, Path]],
    datasets: List[Union[SimpleUrl, Path]],
    producer: str,
):
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
    )
    with rasterio.Env():
        for ds in datasets:
            # ds_path = _normalise_dataset_path(ds)
            logging.info("Processing %s", ds)

            output_uuid, output_path = prepare_and_write(
                ds, collection_location=output_base, producer=producer
            )
            logging.info("Wrote dataset %s to %s", output_uuid, output_path)


if __name__ == "__main__":
    main()

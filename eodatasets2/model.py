import collections.abc
import itertools
import warnings
from collections import defaultdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Tuple, Dict, Optional, Iterable, List, Any, Sequence, Mapping
from uuid import UUID

import affine
import attr
import numpy
import rasterio
import rasterio.features
import shapely
import shapely.affinity
import shapely.ops
from rasterio import DatasetReader
from ruamel.yaml.comments import CommentedMap
from shapely.geometry.base import BaseGeometry

# TODO: these need discussion.
DEA_URI_PREFIX = "https://collections.dea.ga.gov.au"
ODC_DATASET_SCHEMA_URL = "https://schemas.opendatacube.org/dataset"


def nest_properties(d: Mapping[str, Any], separator=":") -> Dict[str, Any]:
    """
    Split keys with embedded colons into sub dictionaries.

    Intended for stac-like properties

    >>> nest_properties({'landsat:path':1, 'landsat:row':2, 'clouds':3})
    {'landsat': {'path': 1, 'row': 2}, 'clouds': 3}
    """
    out = defaultdict(dict)
    for key, val in d.items():
        section, *remainder = key.split(separator, 1)
        if remainder:
            [sub_key] = remainder
            out[section][sub_key] = val
        else:
            out[section] = val

    for key, val in out.items():
        if isinstance(val, dict):
            out[key] = nest_properties(val, separator=separator)

    return dict(out)


class FileFormat(Enum):
    GeoTIFF = 1
    NetCDF = 2


def _dea_uri(product_name, base_uri):
    return f"{base_uri}/product/{product_name}"


@attr.s(auto_attribs=True, slots=True)
class ProductDoc:
    name: str = None
    href: str = None

    @classmethod
    def dea_name(cls, name: str):
        return ProductDoc(name=name, href=_dea_uri(name, DEA_URI_PREFIX))


@attr.s(auto_attribs=True, slots=True, hash=True)
class GridDoc:
    shape: Tuple[int, int]
    transform: Tuple[float, float, float, float, float, float, float, float, float]


@attr.s(auto_attribs=True, slots=True)
class MeasurementDoc:
    path: str
    band: Optional[int] = 1
    layer: Optional[str] = None
    grid: str = "default"

    name: str = attr.ib(metadata=dict(doc_exclude=True), default=None)


class StacPropertyView(collections.abc.Mapping):
    def __init__(self, properties=None) -> None:
        self._props = properties or {}

    @property
    def platform(self) -> str:
        return self._props["eo:platform"]

    @property
    def instrument(self) -> str:
        return self._props["eo:instrument"]

    @property
    def platform_abbreviated(self) -> str:
        """Abbreviated form of a satellite, as used in dea product names. eg. 'ls7'."""
        p = self.platform
        if not p.startswith("landsat"):
            raise NotImplementedError(
                f"TODO: implement non-landsat platform abbreviation " f"(got {p!r})"
            )

        return f"ls{p[-1]}"

    @property
    def producer(self) -> str:
        """
        Organisation that produced the data.

        eg. usgs.gov or ga.gov.au
        """
        return self._props.get("odc:producer")

    @property
    def producer_abbreviated(self) -> Optional[str]:
        """Abbreviated form of a satellite, as used in dea product names. eg. 'ls7'."""
        if not self.producer:
            return None
        producer_domains = {"ga.gov.au": "ga", "usgs.gov": "usgs"}
        try:
            return producer_domains[self.producer]
        except KeyError:
            raise NotImplementedError(
                f"TODO: cannot yet abbreviate organisation domain name {self.producer!r}"
            )

    @producer.setter
    def producer(self, domain: str):
        if "." not in domain:
            warnings.warn(
                "Property 'odc:producer' is expected to be a domain name, "
                "eg 'usgs.gov' or 'ga.gov.au'"
            )
        self._props["odc:producer"] = domain

    @property
    def datetime_range(self):
        return (
            self._props.get("dtr:start_datetime"),
            self._props.get("dtr:end_datetime"),
        )

    @datetime_range.setter
    def datetime_range(self, val: Tuple[datetime, datetime]):
        # TODO: string type conversion, better validation/errors
        start, end = val
        self._props["dtr:start_datetime"] = start
        self._props["dtr:end_datetime"] = end

    @property
    def datetime(self) -> datetime:
        return self._props.get("datetime")

    def __getitem__(self, item):
        return self._props[item]

    def __iter__(self):
        return iter(self._props)

    def __len__(self):
        return len(self._props)

    def __setitem__(self, key, value):
        if key in self._props:
            warnings.warn(f"overridding property {key!r}")
        # TODO: normalise case etc.
        self._props[key] = value

    def nested(self):
        return nest_properties(self._props)


class DeaNamingConventions:
    def __init__(self, properties: StacPropertyView, base_uri: str = None) -> None:
        self.properties = properties
        self.base_uri = base_uri

    @property
    def product_name(self) -> str:
        return self._product_group()

    def _product_group(self, subname=None):
        org_collection_number = self.properties["odc:dataset_version"].split(".")[0]

        # Fallback to the whole product's name
        if not subname:
            subname = self.properties["odc:product_family"]

        p = "{producer}_{platform}{instrument}_{family}_{collection}".format(
            producer=self.properties.producer_abbreviated,
            platform=self.properties.platform_abbreviated,
            instrument=self.properties.instrument[0].lower(),
            family=subname,
            collection=int(org_collection_number),
        )
        return p

    @property
    def product_uri(self) -> Optional[str]:
        if not self.base_uri:
            return None

        return _dea_uri(self.product_name, base_uri=self.base_uri)

    @property
    def dataset_label(self) -> str:
        """
        Label for a dataset
        """
        # TODO: Dataset label Configurability?
        p = self.properties
        version = p["odc:dataset_version"].replace(".", "-")
        return "_".join(
            (
                f"{self.product_name}-{version}",
                p["odc:reference_code"],
                f"{p.datetime:%Y-%m-%d}",
                p["dea:dataset_maturity"],
            )
        )

    def metadata_path(self, work_dir: Path, kind: str = "", suffix: str = "yaml"):
        return self._file(work_dir, kind, suffix)

    def checksum_path(self, work_dir: Path, suffix: str = "sha1"):
        return self._file(work_dir, "", suffix)

    def measurement_file_path(
        self, work_dir: Path, measurement_name: str, suffix: str
    ) -> Path:
        if ":" in measurement_name:
            subgroup, name = measurement_name.split(":")
        else:
            subgroup, name = None, measurement_name
        return self._file(work_dir, name, suffix, sub_name=subgroup)

    def _file(self, work_dir: Path, file_id: str, suffix: str, sub_name: str = None):
        p = self.properties
        version = p["odc:dataset_version"].replace(".", "-")

        if file_id:
            end = f'{p["dea:dataset_maturity"]}_{file_id.replace("_", "-")}.{suffix}'
        else:
            end = f'{p["dea:dataset_maturity"]}.{suffix}'

        return work_dir / "_".join(
            (
                f"{self._product_group(sub_name)}-{version}",
                p["odc:reference_code"],
                f"{p.datetime:%Y-%m-%d}",
                end,
            )
        )


@attr.s(auto_attribs=True, slots=True)
class DatasetDoc:
    id: UUID = None
    product: ProductDoc = None
    locations: List[str] = None

    crs: str = None
    geometry: BaseGeometry = None
    grids: Dict[str, GridDoc] = None

    properties: StacPropertyView = attr.ib(factory=StacPropertyView)

    measurements: Dict[str, MeasurementDoc] = None

    lineage: Dict[str, Sequence[UUID]] = attr.ib(factory=CommentedMap)


def resolve_absolute_offset(
    dataset_path: Path, offset: str, target_path: Optional[Path] = None
) -> str:
    """
    Expand a filename (offset) relative to the dataset.

    >>> external_metadata_loc = Path('/tmp/target-metadata.yaml')
    >>> resolve_absolute_offset(
    ...     Path('/tmp/great_test_dataset'),
    ...     'band/my_great_band.jpg',
    ...     external_metadata_loc,
    ... )
    '/tmp/great_test_dataset/band/my_great_band.jpg'
    >>> resolve_absolute_offset(
    ...     Path('/tmp/great_test_dataset.tar.gz'),
    ...     'band/my_great_band.jpg',
    ...     external_metadata_loc,
    ... )
    'tar:/tmp/great_test_dataset.tar.gz!band/my_great_band.jpg'
    >>> resolve_absolute_offset(
    ...     Path('/tmp/great_test_dataset.tar'),
    ...     'band/my_great_band.jpg',
    ... )
    'tar:/tmp/great_test_dataset.tar!band/my_great_band.jpg'
    >>> resolve_absolute_offset(
    ...     Path('/tmp/MY_DATASET'),
    ...     'band/my_great_band.jpg',
    ...     Path('/tmp/MY_DATASET/ga-metadata.yaml'),
    ... )
    'band/my_great_band.jpg'
    """
    dataset_path = dataset_path.absolute()

    if target_path:
        # If metadata is stored inside the dataset, keep paths relative.
        if str(target_path.absolute()).startswith(str(dataset_path)):
            return offset
    # Bands are inside a tar file

    if ".tar" in dataset_path.suffixes:
        return "tar:{}!{}".format(dataset_path, offset)
    else:
        return str(dataset_path / offset)


class Intern(dict):
    def __missing__(self, key):
        self[key] = key
        return key


def valid_region(
    path: Path, measurements: Iterable[MeasurementDoc], mask_value=None
) -> Tuple[BaseGeometry, Dict[str, GridDoc]]:
    mask = None

    if not measurements:
        raise ValueError("No measurements: cannot calculate valid region")

    measurements_by_grid: Dict[GridDoc, List[MeasurementDoc]] = defaultdict(list)
    mask_by_grid: Dict[GridDoc, numpy.ndarray] = {}

    for measurement in measurements:
        measurement_path = resolve_absolute_offset(path, measurement.path)
        with rasterio.open(str(measurement_path), "r") as ds:
            ds: DatasetReader
            transform: affine.Affine = ds.transform

            if not len(ds.indexes) == 1:
                raise NotImplementedError(
                    f"Only single-band tifs currently supported. File {measurement_path!r}"
                )
            img = ds.read(1)
            grid = GridDoc(shape=ds.shape, transform=transform)
            measurements_by_grid[grid].append(measurement)

            if mask_value is not None:
                new_mask = img & mask_value == mask_value
            else:
                new_mask = img != ds.nodata

            mask = mask_by_grid.get(grid)
            if mask is None:
                mask = new_mask
            else:
                mask |= new_mask
            mask_by_grid[grid] = mask

    grids_by_frequency: List[Tuple[GridDoc, List[MeasurementDoc]]] = sorted(
        measurements_by_grid.items(), key=lambda k: len(k[1])
    )

    def name_grid(grid, measurements: List[MeasurementDoc], name=None):
        name = name or "_".join(m.name for m in measurements)
        for m in measurements:
            m.grid = name

        return name, grid

    grids = dict(
        [
            # most frequent is called "default", others use band names.
            name_grid(*(grids_by_frequency[-1]), name="default"),
            *(name_grid(*g) for g in grids_by_frequency[:-1]),
        ]
    )

    shapes = itertools.chain(
        *[
            rasterio.features.shapes(mask.astype("uint8"), mask=mask)
            for mask in mask_by_grid.values()
        ]
    )
    shape = shapely.ops.unary_union(
        [shapely.geometry.shape(shape) for shape, val in shapes if val == 1]
    )

    # convex hull
    geom = shape.convex_hull

    # buffer by 1 pixel
    geom = geom.buffer(1, join_style=3, cap_style=3)

    # simplify with 1 pixel radius
    geom = geom.simplify(1)

    # intersect with image bounding box
    geom = geom.intersection(shapely.geometry.box(0, 0, mask.shape[1], mask.shape[0]))

    # transform from pixel space into CRS space
    geom = shapely.affinity.affine_transform(
        geom,
        (
            transform.a,
            transform.b,
            transform.d,
            transform.e,
            transform.xoff,
            transform.yoff,
        ),
    )
    # output = shapely.geometry.mapping(geom)
    return geom, grids

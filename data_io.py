import csv
import json
import math
import re
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coverage_model import Grid, Measurement, Site


GRID_FIELDS = (
    'lat_min', 'lat_max', 'lon_min', 'lon_max',
    'grid_origin_lat', 'grid_origin_lon', 'cell_size_m', 'earth_radius_m',
)
MEASUREMENT_COLUMNS = ('lat', 'lon', 'rsrp_dbm', 'timestamp')
SITE_COLUMNS = ('id', 'lat', 'lon')
TIME_OFFSET = re.compile(r'[+-](\d{2})(?::?(\d{2})(?::?(\d{2}))?)?(?:[.,]\d+)?$')


def load_grid(path: Path) -> tuple[Grid, tzinfo]:
    path = Path(path)
    try:
        bounds = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(bounds, dict):
            raise ValueError('expected an object')
        values = {}
        for field in GRID_FIELDS:
            value = bounds.get(field)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f'{field} must be a finite number')
            values[field] = value

        grid = Grid(**values)
        if not -90 <= grid.lat_min < grid.lat_max <= 90:
            raise ValueError('invalid latitude bounds')
        if not -180 <= grid.lon_min < grid.lon_max <= 180:
            raise ValueError('invalid longitude bounds')
        if not (-90 < grid.grid_origin_lat < 90 and -180 <= grid.grid_origin_lon <= 180):
            raise ValueError('invalid grid origin')
        if grid.cell_size_m <= 0 or grid.earth_radius_m <= 0:
            raise ValueError('cell_size_m and earth_radius_m must be positive')

        zone_name = bounds.get('timezone', 'Asia/Bishkek')
        if not isinstance(zone_name, str) or not zone_name:
            raise ValueError('timezone must be an IANA time zone name')
        try:
            local_zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError as error:
            if zone_name != 'Asia/Bishkek':
                raise ValueError(f'time zone {zone_name!r} is unavailable') from error
            # Windows can lack the IANA database. Modern Bishkek uses UTC+06:00.
            local_zone = timezone(timedelta(hours=6), zone_name)
    except (ValueError, OverflowError) as error:
        raise ValueError(f'{path}: {error}') from error
    return grid, local_zone


def _csv_values(line: str) -> list[str]:
    return [value.strip() for value in next(csv.reader([line], strict=True))]


def _read_header(line: str, expected: tuple[str, ...], path: Path) -> list[str]:
    try:
        header = _csv_values(line)
    except csv.Error as error:
        raise ValueError(f'{path}:1: {error}') from error
    if sorted(header) != sorted(expected):
        raise ValueError(f'{path}:1: expected columns {", ".join(expected)}')
    return header


def _read_timestamp(value: str, local_zone: tzinfo) -> datetime:
    time_start = re.search(r'[Tt ]\d{2}', value)
    if not time_start:
        raise ValueError('timestamp must include a date and time')
    offset = TIME_OFFSET.search(value, time_start.end())
    if offset:
        hours, minutes, seconds = offset.groups(default='00')
        if int(hours) >= 24 or int(minutes) >= 60 or int(seconds) >= 60:
            raise ValueError('invalid UTC offset')
    if value.endswith(('Z', 'z')):
        value = value[:-1] + '+00:00'
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=local_zone)
    return timestamp.astimezone(timezone.utc)


def _read_number(value: str) -> float:
    if '_' in value:
        raise ValueError('invalid decimal number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite number')
    return number


def load_measurements(path: Path, grid: Grid, local_zone: tzinfo) -> tuple[list[Measurement], int]:
    path = Path(path)
    measurements = []
    seen = set()
    dropped = 0
    with path.open(encoding='utf-8-sig', newline='') as stream:
        header = _read_header(stream.readline(), MEASUREMENT_COLUMNS, path)
        # A record has four scalar fields. A stray quote must not swallow the next line.
        for line in stream:
            try:
                values = _csv_values(line)
                if len(values) != len(header):
                    raise ValueError('unexpected number of fields')
                row = dict(zip(header, values))
                lat, lon, rsrp = (_read_number(row[name]) for name in ('lat', 'lon', 'rsrp_dbm'))
                if not grid.contains(lat, lon) or not -140 <= rsrp <= -44:
                    raise ValueError('measurement outside the allowed range')
                timestamp = _read_timestamp(row['timestamp'], local_zone)
            except (csv.Error, ValueError, OverflowError):
                dropped += 1
                continue

            key = (lat, lon, rsrp, timestamp)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            measurements.append(Measurement(*key))
    return measurements, dropped


def load_sites(path: Path) -> list[Site]:
    path = Path(path)
    sites = []
    seen = set()
    with path.open(encoding='utf-8-sig', newline='') as stream:
        header = _read_header(stream.readline(), SITE_COLUMNS, path)
        for line_number, line in enumerate(stream, start=2):
            try:
                values = _csv_values(line)
                if len(values) != len(header):
                    raise ValueError('unexpected number of fields')
                row = dict(zip(header, values))
                site_id = row['id']
                if not site_id or site_id in seen:
                    raise ValueError('site ID must be nonempty and unique')
                lat, lon = _read_number(row['lat']), _read_number(row['lon'])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError('invalid site coordinates')
            except (csv.Error, ValueError) as error:
                raise ValueError(f'{path}:{line_number}: {error}') from error
            seen.add(site_id)
            sites.append(Site(site_id, lat, lon))
    return sites

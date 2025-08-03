"""Provides the class MvgApi."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from http import HTTPStatus
from typing import Any

import aiohttp
from furl import furl

MVGAPI_DEFAULT_LIMIT = 10  # API defaults to 10, limits to 100


class Base(Enum):
    """MVG APIs base URLs."""

    BGW = "https://www.mvg.de/api/bgw-pt/v3"
    ZDM = "https://www.mvg.de/.rest/zdm"


class Endpoint(Enum):
    """MVG API endpoints with URLs and arguments."""

    BGW_LOCATION = ("/locations", ["query"])
    BGW_NEARBY = ("/stations/nearby", ["latitude", "longitude"])
    BGW_DEPARTURE = ("/departures", ["globalId", "limit", "offsetInMinutes"])
    BGW_LINES_AT_STATION = ("/lines", ...)
    ZDM_STATION_IDS = ("/mvgStationGlobalIds", ...)
    ZDM_STATIONS = ("/stations", ...)
    ZDM_LINES = ("/lines", ...)


class TransportType(Enum):
    """MVG products defined by the API with name and icon."""

    BAHN = ("Bahn", "mdi:train")
    SBAHN = ("S-Bahn", "mdi:subway-variant")
    UBAHN = ("U-Bahn", "mdi:subway")
    TRAM = ("Tram", "mdi:tram")
    BUS = ("Bus", "mdi:bus")
    REGIONAL_BUS = ("Regionalbus", "mdi:bus")
    SEV = ("SEV", "mdi:taxi")
    SCHIFF = ("Schiff", "mdi:ferry")

    @classmethod
    def all(cls) -> list[TransportType]:
        """Return a list of all products."""
        return [getattr(TransportType, c.name) for c in cls if c.name != "SEV"]


class MvgApiError(Exception):
    """Failed communication with MVG API."""


def _get_minutes_until_departure(departure_time: int) -> int:
    """Calculate the time difference in minutes between the current time and a given departure time.

    Args:
        departure_time: Unix timestamp of the departure time, in seconds.

    Returns:
        The time difference in minutes, as a float.

    """
    current_time = datetime.now()
    departure_datetime = datetime.fromtimestamp(departure_time)
    time_difference = (departure_datetime - current_time).total_seconds()
    minutes_difference = int(time_difference / 60.0)
    return minutes_difference


@dataclass
class DepartureInfo:
    time: int
    planned: int
    delay: int | None
    line: str
    platform: int | None
    realtime: bool
    destination: str
    transport_type: TransportType
    cancelled: bool
    messages: list[str]

    def minutes_until_planned_departure(self) -> int:
        return _get_minutes_until_departure(self.planned)

    def minutes_until_real_departure(self) -> int:
        return _get_minutes_until_departure(self.time)

    @classmethod
    def from_dict(cls, raw_departure_info: dict[str, Any]) -> DepartureInfo:
        return cls(
            time=int(raw_departure_info["realtimeDepartureTime"] / 1000),
            planned=int(raw_departure_info["plannedDepartureTime"] / 1000),
            delay=raw_departure_info.get("delayInMinutes"),
            platform=raw_departure_info.get("platform"),
            realtime=raw_departure_info["realtime"],
            line=raw_departure_info["label"],
            destination=raw_departure_info["destination"],
            transport_type=TransportType[raw_departure_info["transportType"]],
            cancelled=raw_departure_info["cancelled"],
            messages=raw_departure_info["messages"],
        )


@dataclass(frozen=True)
class LineInfo:
    name: str
    transport_type: TransportType
    sev: bool
    diva_id: str

    @classmethod
    def from_dict(cls, raw_line_info: dict[str, Any]) -> LineInfo:
        raw_transport_type = raw_line_info["transportType"]
        raw_line_name = raw_line_info["label"]
        try:
            transport_type = TransportType[raw_transport_type]

        except KeyError:
            raise ValueError(f"Unkown transport type '{raw_transport_type}' for line '{raw_line_name}'")

        return LineInfo(
            name=raw_line_name,
            transport_type=transport_type,
            sev=raw_line_info["sev"],
            diva_id=raw_line_info["divaId"],
        )


class _StationId(str):
    def __init__(self, raw_station_id: str) -> None:
        self._raw_station_id = raw_station_id

    def __str__(self) -> str:
        return self._raw_station_id


@dataclass
class StationInfo:
    station_id: _StationId
    name: str
    place: str
    latitude: float
    longitude: float

    @classmethod
    def from_dict(cls, raw_station_info: dict[str, Any]) -> StationInfo:
        if "id" in raw_station_info:
            raw_station_id = raw_station_info["id"]
        elif "globalId" in raw_station_info:
            raw_station_id = raw_station_info["globalId"]
        else:
            raise ValueError("Invalid station info data: missing key for station ID!")

        station_id = _StationId(raw_station_id)
        return cls(
            station_id=station_id,
            name=raw_station_info["name"],
            place=raw_station_info["place"],
            latitude=raw_station_info["latitude"],
            longitude=raw_station_info["longitude"],
        )


class MvgApi:
    """A class interface to retrieve stations, lines and departures from the MVG.

    The implementation uses the Münchner Verkehrsgesellschaft (MVG) API at https://www.mvg.de.
    It can be instanciated by station name and place or global station id.

    :param station: global station id (e.g. 'de:09162:70')
    :raises MvgApiError: raised on communication failure or unexpected result
    :raises ValueError: raised on bad station id format
    """

    def __init__(self, station_id: _StationId, session: aiohttp.ClientSession | None = None) -> None:
        """Initialize the MVG interface."""
        self._station_id = station_id

        self._session = session

    @property
    def station_id(self) -> _StationId:
        return self._station_id

    @classmethod
    async def create_for_station_async(
        cls,
        station_name_or_id: str,
        session: aiohttp.ClientSession | None = None,
    ) -> MvgApi:
        if cls.valid_station_id(station_name_or_id):
            station_id = _StationId(station_name_or_id.strip())
            return cls(station_id)

        station_info = await cls.station_async(station_name_or_id)
        if station_info is None:
            raise MvgApiError(f"Cannot create API for station {station_name_or_id}: Station not found!")

        return cls(station_info.station_id, session)

    @staticmethod
    def valid_station_id(station_id: str, validate_existance: bool = False) -> bool:
        """Check if the station id is a global station ID according to VDV Recommendation 432.

        :param station_id: a global station id (e.g. 'de:09162:70')
        :param validate_existance: validate the existance in a list from the API
        :return: True if valid, False if Invalid
        """
        valid_format = bool(re.match("de:[0-9]{2,5}:[0-9]+", station_id))
        if not valid_format:
            return False

        if validate_existance:
            try:
                result = asyncio.run(MvgApi.__api(Base.ZDM, Endpoint.ZDM_STATION_IDS))
                if not isinstance(result, list):
                    msg = f"Bad API call: Expected a list, but got {type(result)}."
                    raise MvgApiError(msg)
            except (AssertionError, KeyError) as exc:
                msg = "Bad API call: Could not parse station data."
                raise MvgApiError(msg) from exc
            else:
                return station_id in result

        return True

    @staticmethod
    async def __get(url: furl, session: aiohttp.ClientSession) -> Any:
        try:
            async with session.get(
                url.url,
            ) as resp:
                if resp.status != HTTPStatus.OK:
                    msg = f"Bad API call: Got response ({resp.status}) from {url.url}."
                    raise MvgApiError(msg)
                if resp.content_type != "application/json":
                    msg = f"Bad API call: Got content type {resp.content_type} from {url.url}."
                    raise MvgApiError(msg)
                return await resp.json()

        except aiohttp.ClientError as exc:
            msg = f"Bad API call: Got {type(exc)!s} from {url.url}"
            raise MvgApiError(msg) from exc

    @staticmethod
    async def __api(
        base: Base,
        endpoint: Endpoint | tuple[str, list[str]],
        args: dict[str, Any] | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> Any:  # noqa: ANN401
        """Call the API endpoint with the given arguments.

        :param base: the API base
        :param endpoint: the endpoint
        :param args: a dictionary containing arguments
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: the response as JSON object
        """
        url = furl(base.value)
        url /= endpoint.value[0] if isinstance(endpoint, Endpoint) else endpoint[0]
        url.set(query_params=args)

        if session is not None:
            return await MvgApi.__get(url, session)
        async with aiohttp.ClientSession() as session:
            return await MvgApi.__get(url, session)

    @staticmethod
    async def station_ids_async() -> list[str]:
        """Retrieve a list of all valid station ids.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: station ids as a list
        """
        try:
            result = await MvgApi.__api(Base.ZDM, Endpoint.ZDM_STATION_IDS)
            if not isinstance(result, list):
                msg = f"Bad API call: Expected a list, but got {type(result)}."
                raise MvgApiError(msg)
            return sorted(result)
        except (AssertionError, KeyError) as exc:
            msg = "Bad API call: Could not parse station data."
            raise MvgApiError(msg) from exc

    @staticmethod
    async def stations_async() -> list[dict[str, Any]]:
        """Retrieve a list of all stations.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of stations as dictionary
        """
        try:
            result = await MvgApi.__api(Base.ZDM, Endpoint.ZDM_STATIONS)
            if not isinstance(result, list):
                msg = f"Bad API call: Expected a list, but got {type(result)}."
                raise MvgApiError(msg)
        except (AssertionError, KeyError) as exc:
            msg = "Bad API call: Could not parse station data."
            raise MvgApiError(msg) from exc
        else:
            return result

    @staticmethod
    def stations() -> list[dict[str, Any]]:
        """Retrieve a list of all stations.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of stations as dictionary
        """
        return asyncio.run(MvgApi.stations_async())

    @staticmethod
    async def lines_async() -> list[dict[str, Any]]:
        """Retrieve a list of all lines.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of lines as dictionary
        """
        try:
            result = await MvgApi.__api(Base.ZDM, Endpoint.ZDM_LINES)
            if not isinstance(result, list):
                msg = f"Bad API call: Expected a list, but got {type(result)}."
                raise MvgApiError(msg)
        except (AssertionError, KeyError) as exc:
            msg = "Bad API call: Could not parse station data."
            raise MvgApiError(msg) from exc
        else:
            return result

    @staticmethod
    def lines() -> list[dict[str, Any]]:
        """Retrieve a list of all lines.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of lines as dictionary
        """
        return asyncio.run(MvgApi.lines_async())

    @staticmethod
    async def station_async(query: str) -> StationInfo | None:
        """Find a station by station name and place or global station id.

        :param name: name, place ('Universität, München') or global station id (e.g. 'de:09162:70')
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: the fist matching station as dictionary with keys 'id', 'name', 'place', 'latitude', 'longitude'

        Example result::

            {
                "id": "de:09162:6",
                "name": "Hauptbahnhof",
                "place": "München",
                "latitude": 48.14003,
                "longitude": 11.56107,
            }
        """
        query = query.strip()
        try:
            # return details from ZDM if query is a station id
            if MvgApi.valid_station_id(query):
                stations_endpoint = Endpoint.ZDM_STATIONS.value[0]
                station_endpoint = f"{stations_endpoint}/{query}"
                result = await MvgApi.__api(Base.ZDM, (station_endpoint, []))
                if not isinstance(result, dict):
                    msg = f"Bad API call: Expected a dict, but got {type(result)}."
                    raise MvgApiError(msg)

                return StationInfo.from_dict(result)

            # use open search if query is not a station id
            args = dict.fromkeys(Endpoint.BGW_LOCATION.value[1])
            args.update({"query": query.strip(), "locationTypes": "STATION"})
            result = await MvgApi.__api(Base.BGW, Endpoint.BGW_LOCATION, args)
            if not isinstance(result, list):
                msg = f"Bad API call: Expected a list, but got {type(result)}."
                raise MvgApiError(msg)

            # return first location if lis is not empty
            if len(result) > 0:
                return StationInfo.from_dict(result[0])

        except (AssertionError, KeyError) as exc:
            msg = "Bad API call: Could not parse station data."
            raise MvgApiError(msg) from exc
        else:
            # return None if no station was found
            return None

    @staticmethod
    def station(query: str) -> StationInfo | None:
        """Find a station by station name and place or global station id.

        :param name: name, place ('Universität, München') or global station id (e.g. 'de:09162:70')
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: the fist matching station as dictionary with keys 'id', 'name', 'place', 'latitude', 'longitude'

        Example result::

            {
                "id": "de:09162:6",
                "name": "Hauptbahnhof",
                "place": "München",
                "latitude": 48.14003,
                "longitude": 11.56107,
            }
        """
        return asyncio.run(MvgApi.station_async(query))

    @staticmethod
    async def nearby_async(
        latitude: float,
        longitude: float,
        full_list: bool = True,
    ) -> StationInfo | list[StationInfo] | None:
        """Find the nearest station by coordinates.

        :param latitude: coordinate in decimal degrees
        :param longitude: coordinate in decimal degrees
        :param full_list: return full list of stations instead of a single location
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: the fist matching station as dictionary with keys 'id', 'name', 'place', 'latitude', 'longitude'
            or a list of such station dictionaries, if requested by `full_list` argument

        Example result::

            {"id": "de:09162:70", "name": "Universität", "place": "München", "latitude": 48.15007, "longitude": 11.581}
        """
        try:
            args = dict.fromkeys(Endpoint.BGW_NEARBY.value[1])
            args.update({"latitude": latitude, "longitude": longitude})
            result = await MvgApi.__api(Base.BGW, Endpoint.BGW_NEARBY, args)
            if not isinstance(result, list):
                msg = f"Bad API call: Expected a list, but got {type(result)}."
                raise MvgApiError(msg)

            if len(result) > 0:
                locations = [StationInfo.from_dict(location) for location in result]
                # return full list or only nearest location
                return locations if full_list else locations[0]

        except (AssertionError, KeyError) as exc:
            msg = "Bad API call: Could not parse station data."
            raise MvgApiError(msg) from exc
        else:
            # return None if no station was found
            return None

    @staticmethod
    def nearby(
        latitude: float,
        longitude: float,
        full_list: bool = False,
    ) -> StationInfo | list[StationInfo] | None:
        """Find the nearest station by coordinates.

        :param latitude: coordinate in decimal degrees
        :param longitude: coordinate in decimal degrees
        :param full_list: return full list of stations instead of a single location
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: the fist matching station as dictionary with keys 'id', 'name', 'place', 'latitude', 'longitude'
            or a list of such station dictionaries, if requested by `full_list` argument

        Example result::

            {"id": "de:09162:70", "name": "Universität", "place": "München", "latitude": 48.15007, "longitude": 11.581}
        """
        return asyncio.run(MvgApi.nearby_async(latitude, longitude, full_list))

    async def departures_async(
        self,
        limit: int = MVGAPI_DEFAULT_LIMIT,
        offset: int = 0,
        transport_types: list[TransportType] | None = None,
    ) -> list[DepartureInfo]:
        """Retreive the next departures for a station by station id.

        :param station_id: the global station id ('de:09162:70')
        :param limit: limit of departures, defaults to 10
        :param offset: offset (e.g. walking distance to the station) in minutes, defaults to 0
        :param transport_types: filter by transport type, defaults to None
        :raises MvgApiError: raised on communication failure or unexpected result
        :raises ValueError: raised on bad station id format
        :return: a list of departures as dictionary

        Example result::

            [
                {
                    "time": 1668524580,
                    "planned": 1668524460,
                    "line": "U3",
                    "destination": "Fürstenried West",
                    "type": "U-Bahn",
                    "icon": "mdi:subway",
                    "cancelled": False,
                    "messages": [],
                },
                ...,
            ]
        """
        try:
            args = dict.fromkeys(Endpoint.BGW_DEPARTURE.value[1])
            args.update({"globalId": self.station_id, "offsetInMinutes": offset, "limit": limit})
            if transport_types is None:
                transport_types = TransportType.all()
            args.update({"transportTypes": ",".join([product.name for product in transport_types])})
            result = await MvgApi.__api(Base.BGW, Endpoint.BGW_DEPARTURE, args)
            if not isinstance(result, list):
                msg = f"Bad API call: Expected a list, but got {type(result)}."
                raise MvgApiError(msg)

            departures = [DepartureInfo.from_dict(departure) for departure in result]

        except (AssertionError, KeyError) as exc:
            msg = f"Bad MVG API call: Invalid departure data: {exc}"
            raise MvgApiError(msg) from exc
        else:
            return departures

    def departures(
        self,
        limit: int = MVGAPI_DEFAULT_LIMIT,
        offset: int = 0,
        transport_types: list[TransportType] | None = None,
    ) -> list[DepartureInfo]:
        """Retreive the next departures.

        :param limit: limit of departures, defaults to 10
        :param offset: offset (e.g. walking distance to the station) in minutes, defaults to 0
        :param transport_types: filter by transport type, defaults to None
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of departures as dictionary

        Example result::

            [
                {
                    "time": 1668524580,
                    "planned": 1668524460,
                    "line": "U3",
                    "destination": "Fürstenried West",
                    "type": "U-Bahn",
                    "icon": "mdi:subway",
                    "cancelled": False,
                    "messages": [],
                },
                ...,
            ]

        """
        return asyncio.run(self.departures_async(limit, offset, transport_types))

    async def lines_at_station_async(self) -> set[LineInfo]:
        raw_line_infos = await MvgApi.__api(
            Base.BGW,
            (f"{Endpoint.BGW_LINES_AT_STATION.value[0]}/{self.station_id}", []),
        )

        try:
            return {LineInfo.from_dict(raw_line_info) for raw_line_info in raw_line_infos}
        except ValueError as exc:
            raise MvgApiError("Bad MVG API call: Invalid line data.") from exc

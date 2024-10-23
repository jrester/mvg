"""Provides the class MvgApi."""

from __future__ import annotations

from dataclasses import dataclass

import asyncio
import re
from enum import Enum, auto
from typing import Any
from urllib.parse import urlencode

import aiohttp

MVGAPI_DEFAULT_LIMIT = 10  # API defaults to 10, limits to 100


class Endpoint(Enum):
    """MVG API endpoints with URLs and arguments."""

    ZDM_STATION_IDS = "/mvgStationGlobalIds"
    ZDM_STATIONS = "/stations"
    ZDM_LINES = "/lines"

    BGW_PT_LOCATIONS = "/locations"
    BGW_PT_DEPARTURES = "/departures"
    BGW_PT_LINES = "/lines"


class ApiBase(Enum):
    """MVG APIs base URLs."""

    FIB = "https://www.mvg.de/api/fib/v3"
    ZDM = "https://www.mvg.de/.rest/zdm"
    BGW_PT = "https://www.mvg.de/api/bgw-pt/v3"


class MvgApiError(Exception):
    """Failed communication with MVG API."""


class MvgClient:
    @staticmethod
    async def get(
        api_base: ApiBase, endpoint: Endpoint, query_params: dict[str, str] = {}
    ) -> dict[Any, Any]:
        encoded_query_params = urlencode(query_params)
        url = api_base.value + endpoint.value + "?" + encoded_query_params

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                ) as resp:
                    if resp.status != 200:
                        raise MvgApiError(
                            f"Bad API call: Got response ({resp.status}) from {url}"
                        )
                    if resp.content_type != "application/json":
                        raise MvgApiError(
                            f"Bad API call: Got content type {resp.content_type} from {url}"
                        )
                    return await resp.json()

        except aiohttp.ClientError as exc:
            raise MvgApiError(f"Bad API call: Got {str(type(exc))} from {url}") from exc


class TransportType(Enum):
    """MVG products defined by the API with name and icon."""

    BAHN = auto()
    SBAHN = auto()
    UBAHN = auto()
    TRAM = auto()
    BUS = auto()
    REGIONAL_BUS = auto()
    SEV = auto()
    SCHIFF = auto()

    @staticmethod
    def from_identifier(identifier: str) -> TransportType:
        return TransportType[identifier]

    def get_identifier(self) -> str:
        return self.name

    def get_name(self) -> str:
        match self:
            case TransportType.BAHN:
                return "Bahn"
            case TransportType.SBAHN:
                return "S-Bahn"
            case TransportType.UBAHN:
                return "U-Bahn"
            case TransportType.TRAM:
                return "Tram"
            case TransportType.BUS:
                return "Bus"
            case TransportType.REGIONAL_BUS:
                return "Regionalbus"
            case TransportType.SEV:
                return "SEV"
            case TransportType.SCHIFF:
                return "Schiff"

    def get_icon(self) -> str:
        match self:
            case TransportType.BAHN:
                return "mdi:train"
            case TransportType.SBAHN:
                return "mdi:subway-variant"
            case TransportType.UBAHN:
                return "mdi:subway"
            case TransportType.TRAM:
                return "mdi:tram"
            case TransportType.BUS | TransportType.REGIONAL_BUS:
                return "mdi:bus"
            case TransportType.SEV:
                return "mdi:taxi"
            case TransportType.SCHIFF:
                return "mdi:ferry"

    @classmethod
    def all(cls) -> list[TransportType]:
        """Return a list of all products."""
        return [getattr(TransportType, c.name) for c in cls if c.name != "SEV"]


@dataclass
class Departure:
    time: int
    planned: int
    platform: str | None
    realtime: bool
    line_name: str
    line_id: str
    destination: str
    type: str
    icon: str
    cancelled: bool
    messages: list[str]
    stop_point_global_id: str

    @classmethod
    def from_dict(cls, departure: dict) -> "Departure":
        transport_type = TransportType.from_identifier(departure["transportType"])
        return cls(
            time=int(departure["realtimeDepartureTime"] / 1000),
            planned=int(departure["plannedDepartureTime"] / 1000),
            platform=departure.get("platform"),
            realtime=departure["realtime"],
            line_name=departure["label"],
            line_id=departure["lineId"],
            destination=departure["destination"],
            type=transport_type.get_name(),
            icon=transport_type.get_icon(),
            cancelled=departure["cancelled"],
            messages=departure["messages"],
            stop_point_global_id=departure["stopPointGlobalId"],
        )


class Station:
    def __init__(
        self,
        station_id: str,
        station_name: str,
        place: str,
        latitude: float,
        longitude: float,
    ) -> None:
        self._station_id = station_id
        self._station_name = station_name
        self._place = place
        self._latitude = latitude
        self._longitude = longitude

    @property
    def name(self) -> str:
        return self._station_name

    @property
    def place(self) -> str:
        return self._place

    @property
    def latitude(self) -> float:
        return self._latitude

    @property
    def longitude(self) -> float:
        return self._longitude

    async def departures(
        self,
        limit: int = MVGAPI_DEFAULT_LIMIT,
        offset: int = 0,
        transport_types: list[TransportType] | None = None,
    ) -> list[Departure]:
        if transport_types is None:
            transport_types = TransportType.all()

        transport_types_str = ",".join(
            [transport_type.get_identifier() for transport_type in transport_types]
        )
        args = {
            "globalId": self._station_id,
            "offsetInMinutes": offset,
            "limit": limit,
            "transportTypes": transport_types_str,
        }

        response = await MvgClient.get(ApiBase.BGW_PT, Endpoint.BGW_PT_DEPARTURES, args)
        if not isinstance(response, list):
            raise MvgApiError(
                f"Failed to retrive departures for station {self._station_name}: Reponse is not a list of departures!"
            )

        return [Departure.from_dict(departure_dict) for departure_dict in response]


class MvgApi:
    """A class interface to retrieve stations, lines and departures from the MVG.

    The implementation uses the Münchner Verkehrsgesellschaft (MVG) API at https://www.mvg.de.
    It can be instanciated by station name and place or global station id.

    :param name: name, place ('Universität, München') or global station id (e.g. 'de:09162:70')
    :raises MvgApiError: raised on communication failure or unexpected result
    :raises ValueError: raised on bad station id format
    """

    @staticmethod
    def valid_station_id(station_id: str, validate_existance: bool = False) -> bool:
        """
        Check if the station id is a global station ID according to VDV Recommendation 432.

        :param station_id: a global station id (e.g. 'de:09162:70')
        :param validate_existance: validate the existance in a list from the API
        :return: True if valid, False if Invalid
        """
        valid_format = bool(re.match("de:[0-9]{2,5}:[0-9]+", station_id))
        if not valid_format:
            return False

        if validate_existance:
            try:
                result = asyncio.run(
                    MvgClient.get(ApiBase.ZDM, Endpoint.ZDM_STATION_IDS)
                )
                assert isinstance(result, list)
                return station_id in result
            except (AssertionError, KeyError) as exc:
                raise MvgApiError("Bad API call: Could not parse station data") from exc

        return True

    @staticmethod
    async def station_ids_async() -> list[str]:
        """
        Retrieve a list of all valid station ids.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: station ids as a list
        """
        try:
            result = await MvgClient.get(ApiBase.ZDM, Endpoint.ZDM_STATION_IDS)
            assert isinstance(result, list)
            return sorted(result)
        except (AssertionError, KeyError) as exc:
            raise MvgApiError("Bad API call: Could not parse station data") from exc

    @staticmethod
    async def stations_async() -> list[dict[str, Any]]:
        """
        Retrieve a list of all stations.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of stations as dictionary
        """
        try:
            result = await MvgClient.get(ApiBase.ZDM, Endpoint.ZDM_STATIONS)
            assert isinstance(result, list)
            return result
        except (AssertionError, KeyError) as exc:
            raise MvgApiError("Bad API call: Could not parse station data") from exc

    @staticmethod
    def stations() -> list[dict[str, Any]]:
        """
        Retrieve a list of all stations.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of stations as dictionary
        """
        return asyncio.run(MvgApi.stations_async())

    @staticmethod
    async def lines_async() -> list[dict[str, Any]]:
        """
        Retrieve a list of all lines.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of lines as dictionary
        """
        try:
            result = await MvgClient.get(ApiBase.ZDM, Endpoint.ZDM_LINES)
            assert isinstance(result, list)
            return result
        except (AssertionError, KeyError) as exc:
            raise MvgApiError("Bad API call: Could not parse station data") from exc

    @staticmethod
    def lines() -> list[dict[str, Any]]:
        """
        Retrieve a list of all lines.

        :raises MvgApiError: raised on communication failure or unexpected result
        :return: a list of lines as dictionary
        """
        return asyncio.run(MvgApi.stations_async())

    @staticmethod
    async def station_async(station_name_or_id: str) -> Station | None:
        """
        Find a station by station name and place or global station id.

        :param name: name, place ('Universität, München') or global station id (e.g. 'de:09162:70')
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: the fist matching station as dictionary with keys 'id', 'name', 'place', 'latitude', 'longitude'

        Example result::

            {'id': 'de:09162:6', 'name': 'Hauptbahnhof', 'place': 'München',
                'latitude': 48.14003, 'longitude': 11.56107}
        """
        try:
            result = await MvgClient.get(
                ApiBase.BGW_PT,
                Endpoint.BGW_PT_LOCATIONS,
                {"query": station_name_or_id, "locationType": "STATION"},
            )
            assert isinstance(result, list)

            # return None if result is empty
            if len(result) == 0:
                return None

            # return first location of type "STATION" if name was provided
            return Station(
                station_id=result[0]["globalId"],
                station_name=result[0]["name"],
                place=result[0]["place"],
                latitude=result[0]["latitude"],
                longitude=result[0]["longitude"],
            )

        except (AssertionError, KeyError) as exc:
            raise MvgApiError("Bad API call: Could not parse station data") from exc

    @staticmethod
    def station(station_name_or_id: str) -> Station | None:
        """
        Find a station by station name and place or global station id.

        :param name: name, place ('Universität, München') or global station id (e.g. 'de:09162:70')
        :raises MvgApiError: raised on communication failure or unexpected result
        :return: the fist matching station as dictionary with keys 'id', 'name', 'place', 'latitude', 'longitude'

        Example result::

            {'id': 'de:09162:6', 'name': 'Hauptbahnhof', 'place': 'München',
                'latitude': 48.14003, 'longitude': 11.56107}
        """
        return asyncio.run(MvgApi.station_async(station_name_or_id))

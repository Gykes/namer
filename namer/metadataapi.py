"""
Handle matching data in a FileNameParts (likely generated by a namer_file_parser.py) to
look up metadata (actors, studio, creation data, posters, etc) from the porndb.
"""

import argparse
import itertools
import json
import pathlib
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple
from urllib.parse import quote

import rapidfuzz
from loguru import logger
from PIL import Image
from unidecode import unidecode

from namer.configuration import NamerConfig
from namer.configuration_utils import default_config
from namer.fileutils import make_command, set_permissions
from namer.http import Http
from namer.types import ComparisonResult, FileNameParts, LookedUpFileInfo, Performer


def __find_best_match(query: Optional[str], match_terms: List[str], config: NamerConfig) -> Tuple[str, float]:
    powerset_iter = []
    max_size = min(len(match_terms), config.max_performer_names)
    for length in range(1, max_size + 1):
        data = map(" ".join, itertools.combinations(match_terms, length))
        powerset_iter = itertools.chain(powerset_iter, data)
    ratio = rapidfuzz.process.extractOne(query, choices=powerset_iter)
    return ratio[0], ratio[1] if ratio is not None else ratio


def __attempt_better_match(existing: Tuple[str, float],
                           query: Optional[str],
                           match_terms: List[str],
                           namer_config: NamerConfig) -> Tuple[str, float]:
    if existing is not None and existing[1] >= 89.9:
        return existing
    found = __find_best_match(query, match_terms, namer_config)
    if existing is None:
        return found
    if found is None:
        return "", 0.0
    return existing if existing[1] >= found[1] else found


def __evaluate_match(name_parts: FileNameParts, looked_up: LookedUpFileInfo, namer_config: NamerConfig) -> ComparisonResult:
    site = False
    found_site = None
    if looked_up.site is not None:
        found_site = re.sub(r"[^a-z0-9]", "", looked_up.site.lower())
        if name_parts.site is None:
            site = True
        else:
            site = re.sub(r"[^a-z0-9]", "", name_parts.site.lower()) in found_site or re.sub(r"[^a-z0-9]", "", unidecode(name_parts.site.lower())) in found_site

    if found_site in namer_config.sites_with_no_date_info:
        release_date = True
    else:
        release_date = name_parts.date is not None and (name_parts.date == looked_up.date or unidecode(name_parts.date) == looked_up.date)

    result: Tuple[str, float] = ('', 0.0)

    # Full Name
    all_performers = list(map(lambda p: p.name, looked_up.performers))
    if looked_up.name is not None:
        all_performers.insert(0, looked_up.name)

    result = __attempt_better_match(result, name_parts.name, all_performers, namer_config)
    if name_parts.name is not None:
        result = __attempt_better_match(result, unidecode(name_parts.name), all_performers, namer_config)

    # First Name Powerset.
    if result is not None and result[1] < 89.9:
        all_performers = list(map(lambda p: p.name.split(" ")[0], looked_up.performers))
        if looked_up.name is not None:
            all_performers.insert(0, looked_up.name)
        result = __attempt_better_match(result, name_parts.name, all_performers, namer_config)
        if name_parts.name is not None:
            result = __attempt_better_match(result, unidecode(name_parts.name), all_performers, namer_config)

    return ComparisonResult(
        name=result[0],
        name_match=result[1],
        date_match=release_date,
        site_match=site,
        name_parts=name_parts,
        looked_up=looked_up,
    )


def __update_results(results: List[ComparisonResult],
                     name_parts: FileNameParts,
                     namer_config: NamerConfig,
                     skip_date: bool = False,
                     skip_name: bool = False):
    if len(results) == 0 or not results[0].is_match():
        for match_attempt in __get_metadataapi_net_fileinfo(name_parts, namer_config, skip_date, skip_name):
            result = __evaluate_match(name_parts, match_attempt, namer_config)
            results.append(result)
        results = sorted(results, key=__match_percent, reverse=True)


def __metadata_api_lookup(name_parts: FileNameParts, namer_config: NamerConfig) -> List[ComparisonResult]:
    results = []
    __update_results(results, name_parts, namer_config)
    __update_results(results, name_parts, namer_config, skip_date=True)
    __update_results(results, name_parts, namer_config, skip_date=True, skip_name=True)
    __update_results(results, name_parts, namer_config, skip_name=True)

    if name_parts.date is not None and (len(results) == 0 or not results[-1].is_match()):
        name_parts.date = (date.fromisoformat(name_parts.date) + timedelta(days=-1)).isoformat()
        logger.info("Not found, trying 1 day before: {}", name_parts)
        __update_results(results, name_parts, namer_config)
        __update_results(results, name_parts, namer_config, skip_date=False, skip_name=True)

    if name_parts.date is not None and (len(results) == 0 or not results[-1].is_match()):
        name_parts.date = (date.fromisoformat(name_parts.date) + timedelta(days=2)).isoformat()
        logger.info("Not found, trying 1 day after: {}", name_parts)
        __update_results(results, name_parts, namer_config)
        __update_results(results, name_parts, namer_config, skip_date=False, skip_name=True)
    return results


def __match_percent(result: ComparisonResult) -> float:
    add_value = 0.00
    if result.is_match() is True:
        add_value = 1000.00
    value = (result.name_match + add_value) if result is not None and result.name_match is not None else add_value
    logger.debug("Name match was {:.2f} for {}", value, result.name)
    return value


@logger.catch
def __get_response_json_object(url: str, config: NamerConfig) -> str:
    """
    returns json object with info
    """
    headers = {
        "Authorization": f"Bearer {config.porndb_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "namer-1",
    }
    with Http.get(url, cache_session=config.cache_session, headers=headers) as response:
        response.raise_for_status()
        return response.text


@logger.catch
def download_file(url: str, file: Path, config: NamerConfig) -> bool:
    headers = {
        "User-Agent": "namer-1",
    }
    if "metadataapi.net" in url:
        headers["Authorization"] = f"Bearer {config.porndb_token}"

    http = Http.get(url, cache_session=config.cache_session, headers=headers, stream=True)
    if http.ok:
        with open(file, 'wb') as io_wrapper:
            for data in http.iter_content(1024):
                io_wrapper.write(data)

    return http.ok


@logger.catch
def get_image(url: Optional[str], infix: str, video_file: Optional[Path], config: NamerConfig) -> Optional[Path]:
    """
    returns json object with info
    """
    if url is not None and video_file is not None:
        file = video_file.parent / (video_file.stem + infix + '.png')
        if config.enabled_poster and url.startswith("http") and not file.exists():
            file.parent.mkdir(parents=True, exist_ok=True)
            if download_file(url, file, config):
                with Image.open(file) as img:
                    img.save(file, 'png')
                set_permissions(file, config)
                return file
            else:
                return None
        poster = (video_file.parent / url).resolve()
        return poster if poster.exists() and poster.is_file() else None
    return None


@logger.catch
def get_trailer(url: Optional[str], video_file: Optional[Path], namer_config: NamerConfig) -> Optional[Path]:
    """
    returns json object with info
    """
    if namer_config.trailer_location is not None and not len(namer_config.trailer_location) == 0 and url is not None and len(url) > 0 and video_file is not None:
        logger.info("Attempting to download trailer: {}", url)
        location = namer_config.trailer_location[:max([idx for idx, x in enumerate(namer_config.trailer_location) if x == "."])]
        url_parts = url.split("?")[0].split(".")
        ext = "mp4"
        if url_parts is not None and len(url_parts) > 0 and url_parts[-1].lower() in namer_config.target_extensions:
            ext = url_parts[-1]
        trailer_file: Path = video_file.parent / (location + "." + ext)
        trailer_file.parent.mkdir(parents=True, exist_ok=True)
        if not trailer_file.exists() and url.startswith("http"):
            if download_file(url, trailer_file, namer_config):
                set_permissions(trailer_file, namer_config)
                return trailer_file
            else:
                return None
        trailer = (video_file.parent / url).resolve()
        return trailer if trailer.exists() and trailer.is_file() else None
    return None


def __json_to_fileinfo(data, url, json_response, name_parts) -> LookedUpFileInfo:
    file_info = LookedUpFileInfo()
    file_info.uuid = data._id  # pylint: disable=protected-access
    file_info.name = data.title
    file_info.description = data.description
    file_info.date = data.date
    file_info.source_url = data.url
    file_info.poster_url = data.poster
    file_info.trailer_url = data.trailer
    if data.background is not None:
        file_info.background_url = data.background.large
    file_info.site = data.site.name
    file_info.look_up_site_id = data._id  # pylint: disable=protected-access
    for json_performer in data.performers:
        performer = Performer()
        if hasattr(json_performer, "parent") and hasattr(json_performer.parent, "extras"):
            performer.role = json_performer.parent.extras.gender
        performer.name = json_performer.name
        performer.image = json_performer.image
        file_info.performers.append(performer)
    file_info.original_query = url
    file_info.original_response = json_response
    file_info.original_parsed_filename = name_parts
    tags = []
    if hasattr(data, "tags"):
        for tag in data.tags:
            tags.append(tag.name)
        file_info.tags = tags
    return file_info


def __metadataapi_response_to_data(json_object, url, json_response, name_parts) -> List[LookedUpFileInfo]:
    file_infos = []
    if hasattr(json_object, "data"):
        if isinstance(json_object.data, list):
            for data in json_object.data:
                found_file_info = __json_to_fileinfo(data, url, json_response, name_parts)
                file_infos.append(found_file_info)
        else:
            found_file_info = __json_to_fileinfo(json_object.data, url, json_response, name_parts)
            file_infos.append(found_file_info)
    return file_infos


def __build_url(namer_config: NamerConfig, site: Optional[str] = None, release_date: Optional[str] = None, name: Optional[str] = None, uuid: Optional[str] = None) -> str:
    if uuid is not None:
        query = "/" + str(uuid)
    else:
        query = "?parse="
        if site is not None:
            # There is a known issue in tpdb, where site names are not matched due to casing.
            # example Teens3Some fails, but Teens3some succeeds.  Turns out Teens3Some is treated as 'Teens 3 Some'
            # and Teens3some is treated correctly as 'Teens 3some'.  Also, 'brazzersextra' still match 'Brazzers Extra'
            # Hense, the hack of lower casing the site.
            query += quote(re.sub(r"[^a-z0-9]", "", unidecode(site).lower())) + "."
        if release_date is not None:
            query += release_date + "."
        if name is not None:
            query += quote(re.sub(r" ", ".", name))
        query += "&limit=25"
    return f"{namer_config.override_tpdb_address}scenes{query}"


def __get_metadataapi_net_info(url: str, name_parts: FileNameParts, namer_config: NamerConfig):
    logger.info("Querying: {}", url)
    json_response = __get_response_json_object(url, namer_config)
    file_infos = []
    if json_response is not None and json_response.strip() != "":
        logger.debug("json_response: \n{}", json_response)
        json_obj = json.loads(json_response, object_hook=lambda d: SimpleNamespace(**d))
        formatted = json.dumps(json.loads(json_response), indent=4, sort_keys=True)
        file_infos = __metadataapi_response_to_data(json_obj, url, formatted, name_parts)

    return file_infos


def __get_metadataapi_net_fileinfo(name_parts: FileNameParts, namer_config: NamerConfig, skip_date: bool, skip_name: bool) -> List[LookedUpFileInfo]:
    release_date = name_parts.date if not skip_date else None
    name = name_parts.name if not skip_name else None
    url = __build_url(namer_config, name_parts.site, release_date, name, )
    file_infos = __get_metadataapi_net_info(url, name_parts, namer_config)
    return file_infos


def get_complete_metadatapi_net_fileinfo(name_parts: FileNameParts, uuid: str, namer_config: NamerConfig) -> Optional[LookedUpFileInfo]:
    url = __build_url(namer_config, uuid=uuid)
    file_infos = __get_metadataapi_net_info(url, name_parts, namer_config)
    if len(file_infos) > 0:
        return file_infos[0]
    return None


def match(file_name_parts: Optional[FileNameParts], namer_config: NamerConfig) -> List[ComparisonResult]:
    """
    Give parsed file name parts, and a porndb token, returns a sorted list of possible matches.
    Matches will appear first.
    """
    if file_name_parts is None:
        return []
    comparison_results = __metadata_api_lookup(file_name_parts, namer_config)
    comparison_results = sorted(comparison_results, key=__match_percent, reverse=True)
    # Works around the porndb not returning all info on search queries by looking up the full data
    # with the uuid of the best match.
    if len(comparison_results) > 0 and comparison_results[0].is_match() is True:
        uuid = comparison_results[0].looked_up.uuid
        if uuid is not None:
            file_infos = get_complete_metadatapi_net_fileinfo(file_name_parts, uuid, namer_config)
            if file_infos is not None:
                comparison_results[0].looked_up = file_infos
    return comparison_results


def main(args_list: List[str]):
    """
    Looks up metadata from metadataapi.net base on file name.
    """
    description = """
    Command line interface to look up a suggested name for an adult movie file based on an input string
    that is parsable by namer_file_parser.py
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("-c", "--configfile", help="override location for a configuration file.", type=pathlib.Path)
    parser.add_argument("-f", "--file", help="File we want to provide a match name for.", required=True, type=pathlib.Path)
    parser.add_argument("-j", "--jsonfile", help="write returned json to this file.", type=pathlib.Path)
    parser.add_argument("-v", "--verbose", help="verbose, print logs", action="store_true")
    args = parser.parse_args(args=args_list)
    level = "DEBUG" if args.verbose else "ERROR"
    logger.remove()
    logger.add(sys.stdout, format="{time} {level} {message}", level=level)
    config = default_config()
    file_name = make_command(Path(args.file), config)
    match_results = []
    if file_name is not None and file_name.parsed_file is not None:
        match_results = match(file_name.parsed_file, config)
    if len(match_results) > 0 and match_results[0].is_match() is True:
        print(match_results[0].looked_up.new_file_name(config.inplace_name))
        if args.jsonfile is not None and match_results[0].looked_up is not None and match_results[0].looked_up.original_response is not None:
            Path(args.jsonfile).write_text(match_results[0].looked_up.original_response, encoding="UTF-8")


if __name__ == "__main__":
    main(sys.argv[1:])

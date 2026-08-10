import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.institutions.models import (
    Institution,
    InstitutionSearchItem,
    InstitutionSite,
    InstitutionStatus,
)
from app.institutions.snapshot import verify_snapshot

_PARENTHESES = frozenset("()（）")
_KOREAN_INITIALS = (
    "ㄱ",
    "ㄲ",
    "ㄴ",
    "ㄷ",
    "ㄸ",
    "ㄹ",
    "ㅁ",
    "ㅂ",
    "ㅃ",
    "ㅅ",
    "ㅆ",
    "ㅇ",
    "ㅈ",
    "ㅉ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
_KOREAN_INITIAL_SET = frozenset(_KOREAN_INITIALS)


class UnknownSiteError(LookupError):
    def __init__(self, site_id: str) -> None:
        self.site_id = site_id
        super().__init__(f"unknown or inactive institution site: {site_id}")


@dataclass(frozen=True)
class _SearchRecord:
    institution: Institution
    site: InstitutionSite
    item: InstitutionSearchItem
    normalized_name: str
    terms: tuple[str, ...]
    initial_terms: tuple[str, ...]


class InstitutionStore:
    def __init__(
        self,
        *,
        active_sites: dict[str, InstitutionSite],
        records: tuple[_SearchRecord, ...],
    ) -> None:
        self._active_sites = active_sites
        self._records = records

    @classmethod
    def load(cls, snapshot_root: Path) -> "InstitutionStore":
        verified = verify_snapshot(snapshot_root)
        institutions = {
            institution.institution_id: institution
            for institution in verified.institutions
        }
        active_sites: dict[str, InstitutionSite] = {}
        records: list[_SearchRecord] = []
        for site in verified.sites:
            institution = institutions[site.institution_id]
            if (
                institution.status is not InstitutionStatus.ACTIVE
                or site.status is not InstitutionStatus.ACTIVE
            ):
                continue
            active_sites[site.site_id] = site
            item = InstitutionSearchItem(
                institution_id=institution.institution_id,
                site_id=site.site_id,
                site_name=site.site_name,
                official_name=institution.official_name,
                institution_type=institution.institution_type,
                foundation_type=institution.foundation_type,
                education_office=institution.education_office,
                road_address=site.road_address,
                district=site.district,
                coordinate_quality=site.coordinate_quality,
                snapshot_id=verified.manifest.snapshot_id,
                snapshot_as_of=verified.manifest.snapshot_as_of,
            )
            terms = _deduplicate(
                _normalize(value)
                for value in (
                    institution.official_name,
                    *institution.aliases,
                    site.site_name,
                    institution.official_name + site.site_name,
                    site.site_id,
                )
            )
            records.append(
                _SearchRecord(
                    institution=institution,
                    site=site,
                    item=item,
                    normalized_name=_normalize(institution.official_name),
                    terms=terms,
                    initial_terms=_deduplicate(_initial_consonants(term) for term in terms),
                )
            )
        return cls(active_sites=active_sites, records=tuple(records))

    def search(
        self,
        query: str = "",
        institution_type: str | None = None,
        foundation_type: str | None = None,
        education_office: str | None = None,
        district: str | None = None,
        limit: int = 20,
    ) -> tuple[InstitutionSearchItem, ...]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        if type(query) is not str:
            raise TypeError("query must be a string")
        for filter_name, filter_value in (
            ("institution_type", institution_type),
            ("foundation_type", foundation_type),
            ("education_office", education_office),
            ("district", district),
        ):
            if filter_value is not None and type(filter_value) is not str:
                raise TypeError(f"{filter_name} must be a string or None")
        normalized_query = _normalize(query)
        use_initial_index = bool(normalized_query) and all(
            character in _KOREAN_INITIAL_SET for character in normalized_query
        )
        matches: list[tuple[int, str, str, InstitutionSearchItem]] = []
        for record in self._records:
            if not _matches_filters(
                record,
                institution_type=institution_type,
                foundation_type=foundation_type,
                education_office=education_office,
                district=district,
            ):
                continue
            if not normalized_query:
                rank = 0
            else:
                terms = record.initial_terms if use_initial_index else record.terms
                matched_rank = _match_rank(normalized_query, terms)
                if matched_rank is None:
                    continue
                rank = matched_rank
            matches.append(
                (rank, record.normalized_name, record.site.site_id, record.item)
            )
        matches.sort(key=lambda match: match[:3])
        return tuple(match[3] for match in matches[:limit])

    def require_site(self, site_id: str) -> InstitutionSite:
        if type(site_id) is not str:
            raise TypeError("site_id must be a string")
        site = self._active_sites.get(site_id)
        if site is None:
            raise UnknownSiteError(site_id)
        return site


def _matches_filters(
    record: _SearchRecord,
    *,
    institution_type: str | None,
    foundation_type: str | None,
    education_office: str | None,
    district: str | None,
) -> bool:
    institution = record.institution
    site = record.site
    return (
        (institution_type is None or institution.institution_type == institution_type)
        and (foundation_type is None or institution.foundation_type == foundation_type)
        and (
            education_office is None
            or institution.education_office == education_office
        )
        and (district is None or site.district == district)
    )


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and character not in _PARENTHESES
    )


def _initial_consonants(value: str) -> str:
    result: list[str] = []
    for character in value:
        codepoint = ord(character)
        if 0xAC00 <= codepoint <= 0xD7A3:
            result.append(_KOREAN_INITIALS[(codepoint - 0xAC00) // 588])
        else:
            result.append(character)
    return "".join(result)


def _match_rank(query: str, terms: tuple[str, ...]) -> int | None:
    if any(term == query for term in terms):
        return 0
    if any(term.startswith(query) for term in terms):
        return 1
    if any(query in term for term in terms):
        return 2
    return None


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))

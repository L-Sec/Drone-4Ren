"""Pydantic models shared across the case format, database, and parser contract."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AliasChoices, BaseModel, Field


class DataOrigin(StrEnum):
    """How far the data is from the original bytes."""

    ORIGINAL_RAW = "original_raw"
    MANUFACTURER_EXPORTED = "manufacturer_exported"
    DECRYPTED_PROPRIETARY = "decrypted_proprietary"
    PROCESSED = "processed"
    APP_CACHED = "app_cached"
    CLOUD_DERIVED = "cloud_derived"
    ANALYST_DERIVED = "analyst_derived"
    INFERRED = "inferred"


class WriteBlockerInfo(BaseModel):
    make: str | None = None
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    connection: str | None = None
    test_result: str | None = None


class CaseScope(BaseModel):
    """Optional time and location bounds for analysis.

    ``area_*`` describes a circular area. Events outside it, or outside the
    time range, are flagged for review."""

    start_utc: str | None = None
    end_utc: str | None = None
    area_center_lat: float | None = None
    area_center_lon: float | None = None
    area_radius_m: float | None = None
    locations: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)
    notes: str | None = None


class CaseMeta(BaseModel):
    case_id: str
    case_number: str
    created_utc: str
    collector: str = "operator"
    context: str = Field(
        default="local",
        validation_alias=AliasChoices("context", "legal_authority"),
    )
    description: str | None = None
    scope: CaseScope = Field(default_factory=CaseScope)
    retention_policy: str | None = None
    format_version: int = 1


class EvidenceManifest(BaseModel):
    """Sidecar metadata stored next to each original in the evidence store."""

    sha256: str
    md5: str
    size_bytes: int
    original_name: str
    added_utc: str
    actor: str
    case_number: str
    description: str | None = None
    acquisition_method: str = "logical"
    data_origin: DataOrigin = DataOrigin.ORIGINAL_RAW
    source_device: str | None = None
    write_blocker: WriteBlockerInfo | None = None


class Event(BaseModel):
    """The normalized event emitted by every parser (ARCHITECTURE.md §4).

    Provenance columns (artifact id, parser-run id) are attached by the
    framework at insert time - parsers only describe the event itself.
    Fields the schema does not model must be preserved in ``payload``.
    """

    event_type: str
    source_offset: str | None = None
    ts_utc: str | None = None
    ts_original: str | None = None
    ts_source: str | None = None
    clock_confidence: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt_msl: float | None = None
    alt_agl: float | None = None
    h_acc: float | None = None
    v_acc: float | None = None
    pitch: float | None = None
    roll: float | None = None
    yaw: float | None = None
    speed: float | None = None
    heading: float | None = None
    confidence: float = 1.0
    payload: dict = Field(default_factory=dict)


class FormatSupport(BaseModel):
    vendor: str
    artifact_type: str
    extensions: list[str] = Field(default_factory=list)
    description: str | None = None


class ParserManifest(BaseModel):
    """Self-description every parser plugin must provide (ARCHITECTURE.md §5).

    Feeds the compatibility matrix and the reproducibility appendix.
    """

    name: str
    version: str
    formats: list[FormatSupport] = Field(default_factory=list)
    data_origin: DataOrigin = DataOrigin.ORIGINAL_RAW
    confidence_notes: str | None = None
    known_limitations: list[str] = Field(default_factory=list)
    specificity: int = 50
    """Detection priority: when several parsers detect the same file, the
    highest specificity wins auto-selection. Generic fallbacks (plaintext)
    use low values; format-magic parsers use high values."""
    support_level: str = "full"
    """What this parser actually delivers for the format: "full" (telemetry
    decoded), "partial" (some record types), or "recognition" (format
    identified and metadata surfaced, telemetry NOT decoded). Feeds the
    compatibility matrix."""

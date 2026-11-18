"""The Article 73 field mapping (Phase 5 spec 3.3.4, D18; template v1.0.0, SB-10407).

The report is not the Commission template. It carries `article73_template`: one entry per field
of the template, in template order, with the template's own field number and name, a status and
a value. A value is filled only for a `mapped` or `derivable` field and is null otherwise; a
field the system cannot know is never guessed (REQ-07's "blank, not guessed").

Statuses: mapped (copied from a stored value unchanged), derivable (computed from stored values
by a pinned rule, and the reviewer confirms it), requires_reviewer_input (the reviewer supplies
it; recorded on the submission, never in the assembled bytes), not_available (held only by the
provider's organisation), and section or group for a heading row.

`note` is an additive key (decision 259): a short reason for a field whose status needs one.

A NOT_SERIOUS closure report (spec 3.3.5, D2) omits the fields that do not apply to it.

Pure: no database and no clock.
"""

from dataclasses import dataclass
from typing import Any

TEMPLATE_NAME = "Incident Report for Serious Incidents under the AI Act (High-risk AI systems)"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_REFERENCE = "SB-10407"

STATUSES = (
    "section",
    "group",
    "mapped",
    "derivable",
    "requires_reviewer_input",
    "not_available",
)

# Template 1.2 g options, spelled exactly as the template spells them.
HEALTH = "Harm to a person’s health"
INFRA_MANAGEMENT = "Disruption of the management of critical infrastructure"
INFRA_OPERATION = "Disruption of the operation of critical infrastructure"
RIGHTS = "Infringement of obligations under Union law intended to protect fundamental rights"
PROPERTY = "Harm to property"
ENVIRONMENT = "Harm to environment"
DEATH = "Death"
OTHER = "All other reportable incidents"

# Closure report: the fields that do not apply to it (spec 3.3.5, second column).
_CLOSURE_OMITS = frozenset(
    {
        "1.1 a",
        "1.1 b",
        "1.2 a",
        "1.2 d",
        "1.2 e",
        "1.2 f",
        "1.2 g",
        "3.2 a",
        "3.2 b",
        "3.2 c",
        "4.1 a",
        "4.1 B",
        "4.1 c",
        "4.2 a",
        "4.2 c",
        "Declaration",
    }
)


@dataclass(frozen=True)
class Row:
    field: str
    name: str
    status: str
    note: str | None = None


def _contact(prefix: str, first_letter: str, org_name: str) -> list[Row]:
    """1.3.2, 1.3.3 and 1.3.4: an organisation name and the eleven contact and address fields."""
    names = (
        "Contact’s first name",
        "Contact’s last name",
        "Email",
        "Phone",
        "Country",
        "Street",
        "Street number",
        "Address complement",
        "PO Box",
        "City name",
        "Postal code",
    )
    letters = "abcdefghijkl"
    rows = [Row(f"{prefix} {letters[0]}", org_name, "not_available")]
    for index, name in enumerate(names, start=1):
        rows.append(Row(f"{prefix} {letters[index]}", name, "not_available"))
    return rows


def _rows() -> list[Row]:
    rows = [
        Row("§1", "Section 1: Administrative information", "section"),
        Row(
            "1.1",
            "Responsible market surveillance authority in which country the incident occurred",
            "not_available",
        ),
        Row("1.1 a", "Name of receiving market surveillance authority", "not_available"),
        Row(
            "1.1 b",
            "Reference number assigned by market surveillance authority for this incident",
            "requires_reviewer_input",
            "recorded on the submission",
        ),
        Row("1.2", "Date, type, and classification of incident report", "group"),
        Row(
            "1.2 a",
            "Date of report submission",
            "derivable",
            "known only at submission; see the submission record",
        ),
        Row(
            "1.2 b",
            "Date of incident",
            "derivable",
            "start date only, provisional; the end date requires reviewer input",
        ),
        Row("1.2 c", "Date and time of detection of the incident", "mapped"),
        Row(
            "1.2 d",
            "Manufacturer awareness date of reportability",
            "derivable",
            "first serious-tier assignment; the reviewer confirms it",
        ),
        Row("1.2 e", "Type of report", "requires_reviewer_input", "recorded on the submission"),
        Row(
            "1.2 f",
            "In case of initial and follow-up reports, please indicate the expected date of "
            "the next report",
            "requires_reviewer_input",
            "recorded on the submission",
        ),
        Row(
            "1.2 g",
            "Classification of serious incident",
            "derivable",
            "value lists the candidate options the stored decision supports; the reviewer "
            "picks one",
        ),
        Row("1.3", "Submitter information", "group"),
        Row("1.3.1 a", "Submitter of the report", "not_available"),
        Row("1.3.2", "Provider information", "group"),
        *_contact("1.3.2", "a", "Provider organisation name"),
        Row("1.3.3", "Authorised representative information", "group"),
        *_contact("1.3.3", "a", "Authorised representative organisation name"),
        Row(
            "1.3.4",
            "Submitter’s details if not also provider or authorised representative",
            "group",
        ),
        *_contact("1.3.4", "a", "Registered commercial name of company"),
        Row("1.3.5", "Information about other incident obligations", "not_available"),
        Row("§2", "Section 2: AI System information", "section"),
        Row("2.1 a", "Categorisation of system: EU Database registration id", "not_available"),
        Row(
            "2.2 a",
            "System name (brand/trade/proprietary or common name)",
            "not_available",
            "system_id is our identifier for the system, not its name",
        ),
        Row(
            "2.2 b",
            "Description of the System and its intended purpose",
            "not_available",
        ),
        Row("2.2 c", "Model", "not_available"),
        Row("2.2 d", "Catalogue/reference number", "not_available"),
        Row("2.2 e", "Serial number", "not_available"),
        Row("2.2 f", "Lot/batch number", "not_available"),
        Row("2.2 g", "Software version", "not_available"),
        Row("2.2 h", "Firmware version", "not_available"),
        Row("§3", "Section 3: Incident information", "section"),
        Row(
            "3.1 a",
            "Provide a comprehensive description of the incident, including (1) what went "
            "wrong with the system (if applicable) (2) a description of the effects (if "
            "applicable) and (3)likely causality.",
            "derivable",
            "(1) derivable, (2) mapped from confirmed harm evidence, (3) requires reviewer "
            "input and is never asserted",
        ),
        Row("3.2 a", "Estimated number of users affected", "requires_reviewer_input"),
        Row("3.2 b", "Operator of system at the time of the incident", "not_available"),
        Row("3.2 c", "Remedial actions taken subsequent to the incident", "not_available"),
        Row("3.3", "Initial reporter", "group"),
    ]
    reporter = (
        "Contact’s first name",
        "Contact’s last name",
        "Email",
        "Phone",
        "Country",
        "Street",
        "Street number",
        "Address complement",
        "PO Box",
        "City name",
        "Postal code",
    )
    for index, name in enumerate(reporter):
        rows.append(
            Row(
                f"3.3 {'abcdefghijk'[index]}",
                name,
                "not_available",
                "determination.reviewer_id is the pseudonymous id of the reviewer"
                if index == 0
                else None,
            )
        )
    rows += [
        Row("§4", "Section 4: Provider analysis", "section"),
        Row(
            "4.1 a",
            "For initial and follow-up reports: preliminary results and conclusions of "
            "provider’s investigation",
            "requires_reviewer_input",
            "the system supplies supporting facts in determination and harm_fields",
        ),
        Row(
            "4.1 B",
            "Initial actions (corrective and/or preventive) implemented by the provider",
            "not_available",
        ),
        Row(
            "4.1 c",
            "What further investigations do you intend in view of reaching final conclusions?",
            "not_available",
        ),
        Row(
            "4.2 a",
            "For Final (Serious incident): Description of the providers’s evaluation "
            "concerning possible root causes /causative factors and conclusion",
            "not_available",
        ),
        Row(
            "4.2 b",
            "For Final (Non-reportable incident): Fill out rationale for why this is "
            "considered not reportable",
            "not_available",
            "filled only in a closure report",
        ),
        Row("4.2 c", "Has the risk assessment been reviewed ?", "not_available"),
        Row(
            "§5",
            "Section 5: General comments",
            "derivable",
            "system provenance: report id, version and ledger anchor; the hashes are in the "
            "response headers and the PDF",
        ),
        Row(
            "Declaration",
            "I affirm that the information given above is correct to the best of my knowledge.",
            "not_available",
            "the provider signs",
        ),
    ]
    return rows


ROWS: tuple[Row, ...] = tuple(_rows())


def classification_candidates(tier: str, harm_status: dict[str, str]) -> list[str]:
    """The 1.2 g options the stored decision supports (spec 3.3.4, Pinned). The system never
    chooses one; the reviewer does. NOT_SERIOUS has none."""
    confirmed = {key for key, status in harm_status.items() if status == "CONFIRMED"}
    if tier == "DEATH":
        return [DEATH]
    if tier == "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD":
        options = [INFRA_MANAGEMENT, INFRA_OPERATION]
        if "fundamental_rights_infringement" in confirmed:
            options.append(RIGHTS)
        return options
    if tier == "OTHER_SERIOUS":
        options = []
        if "health_safety_harm" in confirmed:
            options.append(HEALTH)
        if "critical_infrastructure_disruption" in confirmed:
            options += [INFRA_MANAGEMENT, INFRA_OPERATION]
        if "fundamental_rights_infringement" in confirmed:
            options.append(RIGHTS)
        if "property_environmental_damage" in confirmed:
            options += [PROPERTY, ENVIRONMENT]
        return options or [OTHER]
    return []


def build(
    *,
    closure: bool,
    detected_at: str,
    first_serious_assigned_at: str | None,
    candidates: list[str],
    incident_description: dict[str, Any],
    rationale: str | None,
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    """The `article73_template.fields` list for one report, in template order."""
    values: dict[str, Any] = {
        "1.2 b": detected_at[:10],
        "1.2 c": detected_at,
        "1.2 d": first_serious_assigned_at,
        "1.2 g": candidates,
        "3.1 a": incident_description,
        "4.2 b": rationale if closure else None,
        "§5": provenance,
    }
    fields: list[dict[str, Any]] = []
    for row in ROWS:
        if closure and row.field in _CLOSURE_OMITS:
            continue
        status = row.status
        if row.field == "4.2 b" and closure:
            status = "mapped"
        value = values.get(row.field) if status in ("mapped", "derivable") else None
        entry: dict[str, Any] = {
            "field": row.field,
            "name": row.name,
            "status": status,
            "value": value,
        }
        note = row.note
        if row.field == "4.2 b" and closure:
            note = None
        if note is not None:
            entry["note"] = note
        fields.append(entry)
    return fields

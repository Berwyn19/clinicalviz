"""Shared enums. These are the only values the planner (LLM) is allowed to use,
so they double as the "menu" of what the system supports."""

from enum import Enum


class Phase(str, Enum):
    # Values match the ClinicalTrials.gov API v2 exactly.
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"  # not applicable (e.g. device or behavioral trials)


class OverallStatus(str, Enum):
    # Values match the ClinicalTrials.gov API v2 exactly.
    RECRUITING = "RECRUITING"
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    UNKNOWN = "UNKNOWN"


class Intent(str, Enum):
    CHART = "chart"            # aggregate trials into a chart
    TRIAL_LIST = "trial_list"  # no chart needed: return matching trials as a table
    UNSUPPORTED = "unsupported"


class ChartType(str, Enum):
    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    TIME_SERIES = "time_series"
    HISTOGRAM = "histogram"
    SCATTER_PLOT = "scatter_plot"
    NETWORK_GRAPH = "network_graph"
    TABLE = "table"


class Dimension(str, Enum):
    """Category fields a trial can be grouped by."""
    PHASE = "phase"
    START_YEAR = "start_year"
    STATUS = "status"
    COUNTRY = "country"
    LEAD_SPONSOR = "lead_sponsor"
    SPONSOR_CLASS = "sponsor_class"        # INDUSTRY / NIH / OTHER ...
    INTERVENTION_TYPE = "intervention_type"  # DRUG / DEVICE / ...
    INTERVENTION = "intervention"          # normalized drug / treatment name
    CONDITION = "condition"
    STUDY_TYPE = "study_type"              # INTERVENTIONAL / OBSERVATIONAL


# A trial can have several values for these (e.g. many countries, many drugs).
# Only these can be nodes in a network graph.
MULTI_VALUE_DIMENSIONS = {
    Dimension.PHASE,
    Dimension.COUNTRY,
    Dimension.INTERVENTION,
    Dimension.INTERVENTION_TYPE,
    Dimension.CONDITION,
}
NETWORK_NODE_DIMENSIONS = {
    Dimension.LEAD_SPONSOR,
    Dimension.INTERVENTION,
    Dimension.CONDITION,
    Dimension.COUNTRY,
}


class NumericField(str, Enum):
    """Number fields usable for histograms and scatter plots."""
    ENROLLMENT = "enrollment"            # number of participants
    DURATION_MONTHS = "duration_months"  # start date -> completion date
    START_YEAR = "start_year"
"""
Copyright (C) since 2013 Calliope contributors listed in AUTHORS.
Licensed under the Apache 2.0 License (see LICENSE file).

model_run.py
~~~~~~~~~~~~

Preprocessing of model and run configuration into a unified model_run
AttrDict, and building of associated debug information.

"""

import os
import logging
import itertools
import warnings

import pandas as pd

import calliope
from calliope import exceptions
from calliope.core.attrdict import AttrDict
from calliope.core.util.tools import relative_path
from calliope.preprocess import nodes, checks, util

logger = logging.getLogger(__name__)


# Output of: sns.color_palette('cubehelix', 10).as_hex()
_DEFAULT_PALETTE = [
    "#19122b",
    "#17344c",
    "#185b48",
    "#3c7632",
    "#7e7a36",
    "#bc7967",
    "#d486af",
    "#caa9e7",
    "#c2d2f3",
    "#d6f0ef",
]


def model_run_from_yaml(
    model_file, timeseries_dataframes=None, scenario=None, override_dict=None
):
    """
    Generate processed ModelRun configuration from a
    YAML model configuration file.

    Parameters
    ----------
    model_file : str
        Path to YAML file with model configuration.
    timeseries_dataframes : dict, optional
        Dictionary of timeseries dataframes. The keys are strings
        corresponding to the dataframe names given in the yaml files and
        the values are dataframes with time series data.
    scenario : str, optional
        Name of scenario to apply. Can either be a named scenario, or a
        comma-separated list of individual overrides to be combined
        ad-hoc, e.g. 'my_scenario_name' or 'override1,override2'.
    override_dict : dict or AttrDict, optional

    """
    config = AttrDict.from_yaml(model_file)
    config.config_path = model_file

    config_with_overrides, debug_comments, overrides, scenario = apply_overrides(
        config, scenario=scenario, override_dict=override_dict
    )
    subsets = AttrDict.from_yaml(
        os.path.join(os.path.dirname(calliope.__file__), "config", "subsets.yaml")
    )

    return generate_model_run(
        config_with_overrides,
        timeseries_dataframes,
        debug_comments,
        overrides,
        scenario,
        subsets,
    )


def model_run_from_dict(
    config_dict, timeseries_dataframes=None, scenario=None, override_dict=None
):
    """
    Generate processed ModelRun configuration from a
    model configuration dictionary.

    Parameters
    ----------
    config_dict : dict or AttrDict
    timeseries_dataframes : dict, optional
        Dictionary of timeseries dataframes. The keys are strings
        corresponding to the dataframe names given in the yaml files and
        the values are dataframes with time series data.
    scenario : str, optional
        Name of scenario to apply. Can either be a named scenario, or a
        comma-separated list of individual overrides to be combined
        ad-hoc, e.g. 'my_scenario_name' or 'override1,override2'.
    override_dict : dict or AttrDict, optional

    """
    if not isinstance(config_dict, AttrDict):
        config = AttrDict(config_dict)
    else:
        config = config_dict
    config.config_path = None

    config_with_overrides, debug_comments, overrides, scenario = apply_overrides(
        config, scenario=scenario, override_dict=override_dict
    )
    subsets = AttrDict.from_yaml(
        os.path.join(os.path.dirname(calliope.__file__), "config", "subsets.yaml")
    )

    return generate_model_run(
        config_with_overrides,
        timeseries_dataframes,
        debug_comments,
        overrides,
        scenario,
        subsets,
    )


def combine_overrides(config_model, overrides):
    override_dict = AttrDict()
    for override in overrides:
        try:
            yaml_string = config_model.overrides[override].to_yaml()
            override_with_imports = AttrDict.from_yaml_string(yaml_string)
        except KeyError:
            raise exceptions.ModelError(
                "Override `{}` is not defined.".format(override)
            )
        try:
            override_dict.union(override_with_imports, allow_override=False)
        except KeyError as e:
            raise exceptions.ModelError(
                str(e)[1:-1] + ". Already specified but defined again in "
                "override `{}`.".format(override)
            )

    return override_dict


def apply_overrides(config, scenario=None, override_dict=None):
    """
    Generate processed Model configuration, applying any scenarios overrides.

    Parameters
    ----------
    config : AttrDict
        a model configuration AttrDict
    scenario : str, optional
    override_dict : str or dict or AttrDict, optional
        If a YAML string, converted to AttrDict

    """
    debug_comments = AttrDict()

    config_model = AttrDict.from_yaml(
        os.path.join(os.path.dirname(calliope.__file__), "config", "defaults.yaml")
    )

    # Interpret timeseries_data_path as relative
    if "timeseries_data_path" in config.model:
        config.model.timeseries_data_path = relative_path(
            config.config_path, config.model.timeseries_data_path
        )

    # FutureWarning: check if config includes an explicit objective cost class.
    # Added in 0.6.4-dev, to be removed in v0.7.0-dev.
    has_explicit_cost_class = isinstance(
        config.get_key("run.objective_options.cost_class", None), dict
    )

    # The input files are allowed to override other model defaults
    config_model.union(config, allow_override=True)

    # First pass of applying override dict before applying scenarios,
    # so that can override scenario definitions by override_dict
    if override_dict:
        if isinstance(override_dict, str):
            override_dict = AttrDict.from_yaml_string(override_dict)
        elif not isinstance(override_dict, AttrDict):
            override_dict = AttrDict(override_dict)

        warning_messages = checks.check_overrides(config_model, override_dict)
        exceptions.print_warnings_and_raise_errors(warnings=warning_messages)

        # FutureWarning: If config does not include an explicit objective cost class, check override dict.
        # Added in 0.6.4-dev, to be removed in v0.7.0-dev.
        if has_explicit_cost_class is False:
            has_explicit_cost_class = isinstance(
                override_dict.get_key("run.objective_options.cost_class", None), dict
            )

        config_model.union(override_dict, allow_override=True, allow_replacement=True)

    if scenario:
        scenario_overrides = load_overrides_from_scenario(config_model, scenario)
        if not all(i in config_model.get("overrides", {}) for i in scenario_overrides):
            raise exceptions.ModelError(
                "Scenario definition must be a list of override or other scenario names."
            )
        else:
            logger.info(
                "Applying the following overrides from scenario definition: {} ".format(
                    scenario_overrides
                )
            )
        overrides_from_scenario = combine_overrides(config_model, scenario_overrides)

        warning_messages = checks.check_overrides(config_model, overrides_from_scenario)
        exceptions.print_warnings_and_raise_errors(warnings=warning_messages)

        # FutureWarning: If config nor override_dict include an explicit objective cost class, check scenario dict.
        # Added in 0.6.4-dev, to be removed in v0.7.0-dev
        if has_explicit_cost_class is False:
            has_explicit_cost_class = isinstance(
                overrides_from_scenario.get_key(
                    "run.objective_options.cost_class", None
                ),
                dict,
            )

        config_model.union(
            overrides_from_scenario, allow_override=True, allow_replacement=True
        )
        for k, v in overrides_from_scenario.as_dict_flat().items():
            debug_comments.set_key("{}".format(k), "Applied from override")
    else:
        scenario_overrides = []

    # Second pass of applying override dict after applying scenarios,
    # so that scenario-based overrides are overridden by override_dict!
    if override_dict:
        config_model.union(override_dict, allow_override=True, allow_replacement=True)
        for k, v in override_dict.as_dict_flat().items():
            debug_comments.set_key(
                "{}".format(k), "Overridden via override dictionary."
            )

    # FutureWarning: raise cost class warning here.
    # Warning that there will be no default cost class in 0.7.0 #
    # Added in 0.6.4-dev, to be removed in v0.7.0-dev
    if has_explicit_cost_class is False:
        warnings.warn(
            "There will be no default cost class for the objective function in "
            'v0.7.0 (currently "monetary" with a weight of 1). '
            "Explicitly specify the cost class(es) you would like to use "
            'under `run.objective_options.cost_class`. E.g. `{"monetary": 1}` to '
            "replicate the current default.",
            FutureWarning,
        )

    # Drop default nodes, links, and techs
    config_model.del_key("techs.default_tech")
    config_model.del_key("nodes.default_node")
    config_model.del_key("links.default_node_from,default_node_to")

    return config_model, debug_comments, scenario_overrides, scenario


def get_parents(tech_id, model_config):
    """
    Returns the full inheritance tree from which ``tech`` descends,
    ending with its base technology group.

    To get the base technology group,
    use ``get_parents(...)[-1]``.

    Parameters
    ----------
    tech : str
    model_config : AttrDict

    """

    tech = model_config.techs[tech_id].essentials.parent
    parents = [tech]

    while True:
        tech = model_config.tech_groups[tech].essentials.parent
        if tech is None:
            break  # We have reached the top of the chain
        parents.append(tech)
    return parents


def process_techs(config_model):

    default_palette_cycler = itertools.cycle(range(len(_DEFAULT_PALETTE)))

    result = AttrDict()
    errors = []
    debug_comments = AttrDict()

    for tech_id, tech_config in config_model.techs.items():

        # If a tech specifies ``exists: false``, we skip it entirely
        if not tech_config.get("exists", True):
            continue

        tech_result = AttrDict()

        # Add inheritance chain
        tech_result.inheritance = get_parents(tech_id, config_model)

        # CHECK: A tech's parent must lead to one of the built-in tech_groups
        builtin_tech_groups = checks.DEFAULTS.tech_groups.keys()
        if tech_result.inheritance[-1] not in builtin_tech_groups:
            errors.append(
                "tech {} must inherit from a built-in tech group".format(tech_id)
            )

        # Process inheritance
        tech_result.essentials = AttrDict()
        tech_result.constraints = AttrDict()
        for parent in reversed(tech_result.inheritance):
            # Does the parent group have model-wide settings?
            parent_essentials = config_model.tech_groups[parent].essentials
            parent_systemwide_constraints = util.get_systemwide_constraints(
                config_model.tech_groups[parent]
            )
            for k in parent_essentials.as_dict_flat():
                debug_comments.set_key(
                    "{}.essentials.{}".format(tech_id, k),
                    "From parent tech_group `{}`".format(parent),
                )
            tech_result.essentials.union(parent_essentials, allow_override=True)
            tech_result.constraints.union(
                parent_systemwide_constraints, allow_override=True
            )

        # Add this tech's essentials and constraints, overwriting any essentials from parents
        tech_result.essentials.union(tech_config.essentials, allow_override=True)
        tech_result.constraints.union(
            util.get_systemwide_constraints(tech_config), allow_override=True
        )

        # Add allowed_constraints and required_constraints from base tech
        keys_to_add = [
            "required_constraints",
            "allowed_constraints",
            "allowed_costs",
            "allowed_switches",
        ]
        for k in keys_to_add:
            tech_result[k] = config_model.tech_groups[tech_result.inheritance[-1]].get(
                k, []
            )

        # CHECK: If necessary, populate carrier_in and carrier_out in essentials, but
        # also break on missing carrier data
        if "carrier_in" not in tech_result.essentials:
            if tech_result.inheritance[-1] in ["supply", "supply_plus"]:
                pass
            elif tech_result.inheritance[-1] in ["demand", "transmission", "storage"]:
                try:
                    tech_result.essentials.carrier_in = tech_result.essentials.carrier
                    debug_comments.set_key(
                        "{}.essentials.carrier_in".format(tech_id),
                        "Set from essentials.carrier",
                    )
                except KeyError:
                    errors.append(
                        "`carrier` or `carrier_in` must be "
                        "defined for {}".format(tech_id)
                    )
            else:
                errors.append("`carrier_in` must be defined for {}".format(tech_id))

        if "carrier_out" not in tech_result.essentials:
            if tech_result.inheritance[-1] == "demand":
                pass
            elif tech_result.inheritance[-1] in [
                "supply",
                "supply_plus",
                "transmission",
                "storage",
            ]:
                try:
                    tech_result.essentials.carrier_out = tech_result.essentials.carrier
                except KeyError:
                    errors.append(
                        "`carrier` or `carrier_out` must be "
                        "defined for {}".format(tech_id)
                    )
            else:
                errors.append("`carrier_out` must be defined for {}".format(tech_id))
        # Deal with primary carrier in/out for conversion_plus techs
        if tech_result.inheritance[-1] == "conversion_plus":
            for direction in ["_in", "_out"]:
                carriers = set(
                    util.flatten_list(
                        [
                            v
                            for k, v in tech_result.essentials.items()
                            if k.startswith("carrier" + direction)
                        ]
                    )
                )
                primary_carrier = tech_result.essentials.get(
                    "primary_carrier" + direction, None
                )
                if primary_carrier is None and len(carriers) == 1:
                    tech_result.essentials[
                        "primary_carrier" + direction
                    ] = carriers.pop()
                elif primary_carrier is None and len(carriers) > 1:
                    errors.append(
                        "Primary_carrier{0} must be assigned for tech `{1}` as "
                        "there are multiple carriers{0}".format(direction, tech_id)
                    )
                elif primary_carrier not in carriers:
                    errors.append(
                        "Primary_carrier{0} `{1}` not one of the available carriers"
                        "{0} for `{2}`".format(direction, primary_carrier, tech_id)
                    )

        # If necessary, pick a color for the tech, cycling through
        # the hardcoded default palette
        if not tech_result.essentials.get_key("color", None):
            color = _DEFAULT_PALETTE[next(default_palette_cycler)]
            tech_result.essentials.color = color
            debug_comments.set_key(
                "{}.essentials.color".format(tech_id), "From Calliope default palette"
            )
        result[tech_id] = tech_result

    return result, debug_comments, errors


def load_overrides_from_scenario(config_model, scenario):
    def _get_overrides(scenario_name):
        _overrides = config_model.get_key(f"scenarios.{scenario_name}", [scenario_name])
        if isinstance(_overrides, list):
            return _overrides
        else:
            return [_overrides]

    if scenario in config_model.get("scenarios", {}).keys():
        if "," in scenario:
            exceptions.warn(
                f"Scenario name `{scenario}` includes commas that won't be parsed as a list of overrides."
            )
        logger.info("Loading overrides from scenario: {} ".format(scenario))
        scenario_list = _get_overrides(scenario)
    else:
        scenario_list = scenario.split(",")
    scenario_overrides = set()
    for override in scenario_list:
        if isinstance(override, dict):
            raise exceptions.ModelError(
                "Scenario definition must be a list of override or other scenario names."
            )
        if override in config_model.get("scenarios", {}).keys():
            scenario_overrides.update(
                load_overrides_from_scenario(config_model, override)
            )
        else:
            scenario_overrides.add(override)

    return list(scenario_overrides)


def process_tech_groups(config_model, techs):
    tech_groups = AttrDict()
    for group in config_model.tech_groups.keys():
        members = set(k for k, v in techs.items() if group in v.inheritance)
        tech_groups[group] = sorted(list(members))
    return tech_groups


def load_timeseries_from_file(config_model, tskey):
    file_path = os.path.join(config_model.model.timeseries_data_path, tskey)
    df = pd.read_csv(file_path, index_col=0)
    df.columns = pd.MultiIndex.from_product(
        [[tskey], df.columns], names=["source", "column"]
    )
    return df


def check_timeseries_dataframes(timeseries_dataframes):
    """
    Timeseries dataframes should be dict of pandas DataFrames.
    """
    if not isinstance(timeseries_dataframes, dict) or not all(
        [
            isinstance(timeseries_dataframes[i], pd.DataFrame)
            for i in timeseries_dataframes
        ]
    ):
        raise exceptions.ModelError(
            "Error in loading timeseries data from dataframes. "
            "`timeseries_dataframes` must be dict of pandas DataFrames."
        )


def load_timeseries_from_dataframe(timeseries_dataframes, tskey):

    # If `df=` is called, timeseries_dataframes must be entered
    if timeseries_dataframes is None:
        raise exceptions.ModelError(
            "Error in loading timeseries. Model config specifies df={} but "
            "no timeseries passed as arguments in calliope.Model(...). "
            "Note that, if running from a command line, it is not possible "
            "to read dataframes via `df=...` and you should specify "
            "`file=...` with a CSV file.".format(tskey)
        )

    try:
        df = timeseries_dataframes[tskey]
    except KeyError:
        raise exceptions.ModelError(
            "Error in loading data from dataframe. "
            "Model attempted to load dataframe with key `{}`, "
            "but available dataframes are {}".format(
                tskey, set(timeseries_dataframes.keys())
            )
        )
    df.columns = pd.MultiIndex.from_product(
        [[tskey], df.columns], names=["source", "column"]
    )
    return df


def _parser(x, dtformat):
    return pd.to_datetime(x, format=dtformat, exact=False)


def _get_names(config):
    """
    Find names of csv files (file=) or dataframes (df=) called in config
    """
    tsnames = []
    tsvars = []
    for k, v in config.items():
        if "=" in str(v):
            tsnames.append((v.split("=")[0], v.split("=")[1].rsplit(":", 1)[0]))
            if ".costs." in k:
                tsvars.append(f"cost_{k.split('.')[-1]}")
            elif ".carrier_ratios." in k:
                tsvars.append("carrier_ratios")
            else:
                tsvars.append(k.split(".")[-1])
    return set(tsnames), set(tsvars)


def process_timeseries_data(config_model, model_run, timeseries_dataframes):

    timeseries_data = config_model.model.get("timeseries_data", None)

    dtformat = config_model.model["timeseries_dateformat"]

    # Generate set of all files and dataframes we want to load
    node_config = model_run.nodes.as_dict_flat()
    model_config = config_model.model.as_dict_flat()

    constraint_tsnames, constraint_tsvars = _get_names(node_config)
    cluster_tsnames, cluster_tsvars = _get_names(model_config)

    # Check if timeseries_dataframes is in the correct format (dict of
    # pandas DataFrames)
    if timeseries_dataframes is not None:
        check_timeseries_dataframes(timeseries_dataframes)

    # Check there is at least one timeseries present
    if len(constraint_tsnames) == 0:
        raise exceptions.ModelError(
            "There is no timeseries in the model. At least one timeseries is "
            "necessary to run the model."
        )

    # Load each timeseries into timeseries data. tskey is either a filename
    # (called by file=...) or a key in timeseries_dataframes (called by df=...)
    for tskey in constraint_tsnames | cluster_tsnames:
        # If tskey is a CSV path, load the CSV, else load the dataframe

        if tskey[0] == "file":
            df = load_timeseries_from_file(config_model, tskey[1])
        elif tskey[0] == "df":
            df = load_timeseries_from_dataframe(timeseries_dataframes, tskey[1])
        else:
            raise KeyError(f"Unrecognised timeseries data source {tskey[0]}")

        try:
            df.apply(pd.to_numeric)
        except ValueError as e:
            raise exceptions.ModelError(
                "Error in loading data from {}. Ensure all entries are "
                "numeric. Full error: {}".format(tskey[1], e)
            )
        # Parse the dates, checking for errors specific to this
        try:
            df.index = _parser(df.index, dtformat)
        except ValueError as e:
            raise exceptions.ModelError(
                "Error in parsing dates in timeseries data from {}, "
                "using datetime format `{}`: {}".format(tskey[1], dtformat, e)
            )
        if timeseries_data is None:
            timeseries_data = df
        else:
            timeseries_data = pd.concat([timeseries_data, df], axis=1)

    # Apply time subsetting, if supplied in model_run
    subset_time_config = config_model.model.get("subset_time", None)
    if subset_time_config is not None:
        # Test parsing dates first, to make sure they fit our required subset format
        try:
            subset_time = _parser(subset_time_config, "%Y-%m-%d %H:%M:%S")
        except ValueError as e:
            raise exceptions.ModelError(
                "Timeseries subset must be in ISO format (anything up to the  "
                "detail of `%Y-%m-%d %H:%M:%S`.\n User time subset: {}\n "
                "Error caused: {}".format(subset_time_config, e)
            )
        if isinstance(subset_time_config, list) and len(subset_time_config) == 2:
            time_slice = slice(subset_time_config[0], subset_time_config[1])

            # Don't allow slicing outside the range of input data
            if (
                pd.Timestamp(subset_time[0].date()) < timeseries_data.index[0]
                or pd.Timestamp(subset_time[1].date()) > timeseries_data.index[-1]
            ):
                raise exceptions.ModelError(
                    "subset time range {} is outside the input data time range "
                    "[{}, {}]".format(
                        subset_time_config,
                        timeseries_data.index[0].strftime("%Y-%m-%d"),
                        timeseries_data.index[-1].strftime("%Y-%m-%d"),
                    )
                )
        else:
            raise exceptions.ModelError(
                "subset_time must be a list of two datetime strings, not: {}".format(
                    subset_time_config
                )
            )

        timeseries_data = timeseries_data.loc[time_slice, :]
        if timeseries_data.empty:
            raise exceptions.ModelError(
                "The time slice {} creates an empty timeseries array.".format(
                    time_slice
                )
            )

    if timeseries_data[[i[1] for i in constraint_tsnames]].isna().any().any():
        raise exceptions.ModelError(
            "Missing data for the timeseries array(s) {}.".format(
                timeseries_data.columns[timeseries_data.isna().any()].values
            )
        )
    if (
        cluster_tsnames
        and timeseries_data[[i[1] for i in cluster_tsnames]]
        .resample("1D")
        .mean()
        .isna()
        .any()
        .any()
    ):
        raise exceptions.ModelError(
            "Missing data for the timeseries array(s) {}.".format(
                timeseries_data.columns[timeseries_data.isna().any()].values
            )
        )

    return timeseries_data.rename_axis(index="timesteps"), constraint_tsvars


def generate_model_run(
    config,
    timeseries_dataframes,
    debug_comments,
    applied_overrides,
    scenario,
    subsets,
):
    """
    Returns a processed model_run configuration AttrDict and a debug
    YAML object with comments attached, ready to write to disk.

    Parameters
    ----------
    config : AttrDict
    timeseries_dataframes : dict
    debug_comments : AttrDict
    scenario : str
    """
    model_run = AttrDict()
    model_run["scenario"] = scenario
    model_run["applied_overrides"] = ";".join(applied_overrides)

    # 1) Initial checks on model configuration
    warning_messages, errors = checks.check_initial(config)
    exceptions.print_warnings_and_raise_errors(warnings=warning_messages, errors=errors)

    # 2) Fully populate techs
    # Raises ModelError if necessary
    model_run["techs"], debug_techs, errors = process_techs(config)
    debug_comments.set_key("model_run.techs", debug_techs)
    exceptions.print_warnings_and_raise_errors(errors=errors)

    # 3) Fully populate tech_groups
    model_run["tech_groups"] = process_tech_groups(config, model_run["techs"])

    # 4) Fully populate nodes
    (
        model_run["nodes"],
        debug_nodes,
        warning_messages,
        errors,
    ) = nodes.process_nodes(config, model_run["techs"])
    debug_comments.set_key("model_run.nodes", debug_nodes)
    exceptions.print_warnings_and_raise_errors(warnings=warning_messages, errors=errors)

    # 5) Fully populate timeseries data
    # Raises ModelErrors if there are problems with timeseries data at this stage
    (
        model_run["timeseries_data"],
        model_run["timeseries_vars"],
    ) = process_timeseries_data(config, model_run, timeseries_dataframes)

    # 6) Grab additional relevant bits from run and model config
    model_run["run"] = config["run"]
    model_run["model"] = config["model"]

    # model_run["sets"] = all_sets
    model_run["subsets"] = subsets
    # model_run["constraint_sets"] = constraint_sets.generate_constraint_sets(model_run)

    # 8) Final sense-checking
    final_check_comments, warning_messages, errors = checks.check_final(model_run)
    debug_comments.union(final_check_comments)
    exceptions.print_warnings_and_raise_errors(warnings=warning_messages, errors=errors)

    # 9) Build a debug data dict with comments and the original configs
    debug_data = AttrDict(
        {
            "comments": debug_comments,
            "config_initial": config,
        }
    )

    return model_run, debug_data

import argparse
import errno
import importlib
import io
import os
from typing import List

from configobj import ConfigObj
from mache import MachineInfo
from validate import Validator

from zppy.bundle import Bundle, predefined_bundles
from zppy.climo import climo
from zppy.e3sm_diags import e3sm_diags
from zppy.global_time_series import global_time_series
from zppy.ilamb import ilamb
from zppy.mpas_analysis import mpas_analysis
from zppy.tc_analysis import tc_analysis
from zppy.ts import ts
from zppy.utils import checkStatus, submitScript


# FIXME: C901 'main' is too complex (19)
def main():  # noqa: C901

    # Command line parser
    parser = argparse.ArgumentParser(
        description="Launch E3SM post-processing tasks", usage="zppy -c <config>"
    )
    parser.add_argument(
        "-c", "--config", type=str, help="configuration file", required=True
    )
    parser.add_argument(
        "-l", "--last-year", type=int, help="last year to process", required=False
    )
    args = parser.parse_args()

    # Subdirectory where templates are located
    templateDir = os.path.join(os.path.dirname(__file__), "templates")

    # Read configuration file and validate it
    default_config = os.path.join(templateDir, "default.ini")
    user_config = ConfigObj(args.config, configspec=default_config)

    # Load all external plugins. Build a list.
    plugins = []
    if "plugins" in user_config["default"].keys():
        for plugin_name in user_config["default"]["plugins"]:
            # Load plugin module
            try:
                plugin_module = importlib.import_module(plugin_name)
            except BaseException:
                raise ValueError(
                    "Could not load external zppy plugin module {}".format(plugin_name)
                )
            # Path
            plugin_path = plugin_module.__path__[0]
            # Add to list
            plugins.append(
                {"name": plugin_name, "module": plugin_module, "path": plugin_path}
            )

    # Read configuration files again, this time including all plugins
    with open(default_config) as f:
        default = f.read()
    for plugin in plugins:
        # Read plugin 'default.ini' if it exists
        plugin_default_file = os.path.join(plugin["path"], "templates/default.ini")
        print(plugin_default_file)
        if os.path.isfile(plugin_default_file):
            with open(plugin_default_file) as f:
                default += "\n" + f.read()
    user_config = ConfigObj(args.config, configspec=io.StringIO(default))

    # Handle 'campaign' option
    if "campaign" in user_config["default"]:
        campaign = user_config["default"]["campaign"]
    else:
        campaign = "none"
    if campaign != "none":
        campaign_file = os.path.join(templateDir, "{}.cfg".format(campaign))
        if not os.path.exists(campaign_file):
            raise ValueError(
                "{} does not appear to be a known campaign".format(campaign)
            )
        campaign_config = ConfigObj(campaign_file, configspec=default_config)
        # merge such that user_config takes priority over campaign_config
        campaign_config.merge(user_config)
        config = campaign_config
    else:
        config = user_config

    # Validate
    _validate_config(config)

    # Add templateDir to config
    config["default"]["templateDir"] = templateDir

    # Output script directory
    output = config["default"]["output"]
    username = os.environ.get("USER")
    output = output.replace("$USER", username)
    scriptDir = os.path.join(output, "post/scripts")
    job_ids_file = os.path.join(scriptDir, "jobids.txt")
    try:
        os.makedirs(scriptDir)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise OSError("Cannot create script directory")
        pass

    if ("machine" not in config["default"]) or (config["default"]["machine"] == ""):
        # MachineInfo below will then call `discover_machine()`,
        # which only works on log-in nodes.
        machine = None
    else:
        # If `machine` is set, then MachineInfo can bypass the
        # `discover_machine()` function.
        machine = config["default"]["machine"]
    machine_info = MachineInfo(machine=machine)
    default_machine = machine_info.machine
    (
        default_account,
        default_partition,
        default_constraint,
        _,
    ) = machine_info.get_account_defaults()
    unified_base = machine_info.config.get("e3sm_unified", "base_path")
    config["default"]["diagnostics_base_path"] = machine_info.config.get(
        "diagnostics", "base_path"
    )
    config["default"]["web_portal_base_path"] = machine_info.config.get(
        "web_portal", "base_path"
    )
    config["default"]["web_portal_base_url"] = machine_info.config.get(
        "web_portal", "base_url"
    )

    # Determine machine to decide which header files to use
    if ("machine" not in config["default"]) or (config["default"]["machine"] == ""):
        config["default"]["machine"] = default_machine
    # Determine account
    if config["default"]["account"] == "":
        if default_account:
            config["default"]["account"] = default_account
        elif config["default"]["machine"] in ["compy", "pm-cpu", "pm-gpu", "chrysalis"]:
            config["default"]["account"] = "e3sm"
        elif config["default"]["machine"] == "anvil":
            config["default"]["account"] = "condo"
        else:
            raise ValueError(f"Invalid machine {config['default']['machine']}")
    # Determine partition
    if ("partition" not in config["default"]) or (config["default"]["partition"] == ""):
        config["default"]["partition"] = default_partition
    if ("constraint" not in config["default"]) or (
        config["default"]["constraint"] == ""
    ):
        config["default"]["constraint"] = default_constraint
    if config["default"]["machine"] in ["pm-cpu", "pm-gpu"]:
        constraint = config["default"]["constraint"]
        if constraint not in ["cpu", "gpu"]:
            raise ValueError(
                f'Expected Perlmutter constraint to be "cpu" or '
                f'"gpu" but got: {constraint}'
            )
    # Determine environment_commands
    if config["default"]["environment_commands"] == "":
        machine = config["default"]["machine"]
        config["default"][
            "environment_commands"
        ] = f"source {unified_base}/load_latest_e3sm_unified_{machine}.sh"

    if args.last_year:
        config["default"]["last_year"] = args.last_year

    existing_bundles: List[Bundle] = []

    # predefined bundles
    existing_bundles = predefined_bundles(config, scriptDir, existing_bundles)

    # climo tasks
    existing_bundles = climo(config, scriptDir, existing_bundles, job_ids_file)

    # time series tasks
    existing_bundles = ts(config, scriptDir, existing_bundles, job_ids_file)

    # tc_analysis tasks
    existing_bundles = tc_analysis(config, scriptDir, existing_bundles, job_ids_file)

    # e3sm_diags tasks
    existing_bundles = e3sm_diags(config, scriptDir, existing_bundles, job_ids_file)

    # mpas_analysis tasks
    existing_bundles = mpas_analysis(config, scriptDir, existing_bundles, job_ids_file)

    # global time series tasks
    existing_bundles = global_time_series(
        config, scriptDir, existing_bundles, job_ids_file
    )

    # ilamb tasks
    existing_bundles = ilamb(config, scriptDir, existing_bundles, job_ids_file)

    # zppy external plugins
    for plugin in plugins:
        # Get plugin module function
        plugin_func = getattr(plugin["module"], plugin["name"])
        # Call plugin
        existing_bundles = plugin_func(
            plugin["path"], config, scriptDir, existing_bundles, job_ids_file
        )

    # Submit bundle jobs
    for b in existing_bundles:
        skip = checkStatus(b.bundle_status)
        if skip:
            continue
        b.display_dependencies()
        b.render(config)
        if not b.dry_run:
            submitScript(
                b.bundle_file,
                b.bundle_status,
                b.export,
                job_ids_file,
                dependFiles=b.dependencies_external,
            )


def _validate_config(config):
    validator = Validator()

    result = config.validate(validator)
    if result is not True:
        print("Validation results={}".format(result))
        raise ValueError(
            "Configuration file validation failed. Parameters listed as false in the validation results have invalid values."
        )
    else:
        print("Configuration file validation passed.")

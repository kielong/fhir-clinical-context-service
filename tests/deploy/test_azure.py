# ORIGIN: AI — test cases typed by Claude Code from the agreed deployment rules (one VM running the
#   same compose file; only the deployer's address may connect; the data arrives as a dump and is
#   restored before HAPI starts; a random database password; nothing that is not committed is
#   shipped), reviewed by Kiel. Cases marked (AI) are Claude Code's additions, not yet adopted.
"""The Azure deployment files: what can be checked without an Azure account.

None of this creates anything in the cloud. The templates and scripts are checked for the rules
that keep a deployment safe (who may connect, what is exposed, how secrets are made), the scripts
are run in dry-run mode with a stand-in `az` that fails the test if it is ever called, and the
order of the restore is pinned.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent.parent
AZURE = ROOT / "azure"
BICEP = AZURE / "main.bicep"
SCRIPTS = ["deploy.sh", "remote-setup.sh", "make-dump.sh", "teardown.sh"]
GOOD_IP = "203.0.113.7"


def _bicep() -> str:
    return BICEP.read_text()


def _run(script: str, *args: str, stdin: str = "", extra_path: Path | None = None):
    """Run one of the scripts with a PATH that has a tripwire `az` in front, so a test fails if a
    dry run or a refusal ever reaches the cloud."""
    env = dict(os.environ)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(AZURE / script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=60,
    )


@pytest.fixture
def tripwire(tmp_path):
    """A fake `az` (and `ssh`, `scp`) that records being called and fails."""
    calls = tmp_path / "calls.txt"
    for name in ("az", "ssh", "scp"):
        fake = tmp_path / name
        fake.write_text(f'#!/bin/sh\necho "{name} $@" >> "{calls}"\nexit 97\n')
        fake.chmod(0o755)
    return tmp_path, calls


# ---- who may connect


def test_the_template_asks_who_may_connect_and_has_no_default_for_it():
    line = next(x for x in _bicep().splitlines() if x.startswith("param allowedSourceIp"))

    assert "=" not in line  # a default would be a deployment that is open unless you noticed


def test_nothing_in_the_template_is_open_to_everyone():
    text = _bicep()

    for wide_open in ("'*'", "'Internet'", "'0.0.0.0/0'", "'Any'", "'AzureCloud'"):
        assert wide_open not in text, wide_open


def test_every_inbound_allow_rule_is_limited_to_the_deployers_address():
    text = _bicep()

    allows = text.count("access: 'Allow'")
    limited = len(re.findall(r"sourceAddressPrefix:\s*allowedSourceIp\b", text))
    assert allows >= 3
    assert limited == allows


def test_only_ssh_the_api_and_hapi_are_opened_never_the_model_or_the_database():
    ports = set(re.findall(r"destinationPortRange:\s*'(\d+)'", _bicep()))

    assert ports == {"22", "8000", "8080"}
    assert "11434" not in _bicep() and "5432" not in _bicep()


def test_password_login_is_off_so_only_the_key_gets_in():
    assert "disablePasswordAuthentication: true" in _bicep()
    assert "adminPassword" not in _bicep()


# ---- the machine


def test_the_disk_is_large_enough_for_the_images_the_models_and_the_database():
    # Measured: 3.9 GB of Postgres, about 7 GB of images, 3.3 GB for the model, plus a dump.
    size = int(re.search(r"diskSizeGB:\s*(\d+)", _bicep()).group(1))

    assert size >= 64


def test_the_machine_shuts_itself_down_every_day_so_it_cannot_be_forgotten_running():
    text = _bicep()

    assert "ComputeVmShutdownTask" in text
    assert "status: 'Enabled'" in text
    assert "dailyRecurrence" in text


def test_the_address_is_static_so_the_source_links_survive_a_restart():
    assert "publicIPAllocationMethod: 'Static'" in _bicep()


# (AI) The compiled template is the truth, so when a Bicep compiler is available it is checked too.
def _compiled() -> dict:
    compiler = shutil.which("bicep") or str(Path.home() / ".azure" / "bin" / "bicep")
    if not Path(compiler).exists():
        pytest.skip("no Bicep compiler here; CI compiles the template with `az bicep build`")
    out = subprocess.run(
        [compiler, "build", str(BICEP), "--stdout"], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_compiled_template_agrees_no_rule_is_open_and_no_password_is_set():
    template = _compiled()

    rules = [
        rule["properties"]
        for resource in template["resources"]
        if resource["type"] == "Microsoft.Network/networkSecurityGroups"
        for rule in resource["properties"]["securityRules"]
    ]
    assert rules
    for rule in rules:
        assert rule["direction"] == "Inbound" and rule["access"] == "Allow"
        assert rule["sourceAddressPrefix"] == "[parameters('allowedSourceIp')]"
    vm = next(r for r in template["resources"] if r["type"] == "Microsoft.Compute/virtualMachines")
    assert vm["properties"]["osProfile"]["linuxConfiguration"]["disablePasswordAuthentication"]
    assert "allowedSourceIp" in template["parameters"]
    assert "defaultValue" not in template["parameters"]["allowedSourceIp"]


# ---- setting the machine up


def test_the_first_boot_installs_docker_and_makes_the_folder_the_scripts_copy_into():
    text = (AZURE / "cloud-init.yaml").read_text()
    config = yaml.safe_load(text)

    assert text.startswith("#cloud-config")
    joined = " ".join(str(step) for step in config["runcmd"])
    assert "docker-compose-plugin" in joined
    assert "/opt/clinical-context" in joined
    assert "docker" in joined and "azureuser" in joined  # the deploy user can run docker


def test_the_template_hands_the_first_boot_file_to_the_machine():
    assert "loadFileAsBase64('cloud-init.yaml')" in _bicep()


# ---- the scripts


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_script_is_valid_shell(script):
    result = subprocess.run(["bash", "-n", str(AZURE / script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_a_deploy_will_not_start_without_being_told_who_may_connect(tripwire):
    path, calls = tripwire

    result = _run("deploy.sh", extra_path=path)

    assert result.returncode != 0
    assert "--my-ip" in result.stderr
    assert not calls.exists()  # nothing was even attempted


@pytest.mark.parametrize(
    "address", ["0.0.0.0/0", "0.0.0.0", "*", "10.0.0.0/8", "203.0.113.0/16", "not-an-ip", "1.2.3"]
)
def test_a_deploy_refuses_an_address_that_would_open_it_to_the_world(tripwire, address):
    path, calls = tripwire

    result = _run("deploy.sh", "--my-ip", address, "--dry-run", extra_path=path)

    assert result.returncode != 0
    assert "address" in result.stderr.lower()
    assert not calls.exists()


@pytest.mark.parametrize("address", [GOOD_IP, "203.0.113.0/24", "198.51.100.9/32"])
def test_a_single_address_or_a_small_network_is_accepted(tripwire, address):
    path, calls = tripwire

    result = _run("deploy.sh", "--my-ip", address, "--dry-run", extra_path=path)

    assert result.returncode == 0, result.stderr
    assert not calls.exists()


def test_a_dry_run_prints_every_step_and_runs_none_of_them(tripwire):
    path, calls = tripwire

    result = _run("deploy.sh", "--my-ip", GOOD_IP, "--dry-run", extra_path=path)

    out = result.stdout
    assert result.returncode == 0, result.stderr
    assert not calls.exists()  # neither az, ssh nor scp was called
    assert "az group create" in out
    assert "az deployment group create" in out
    assert f"allowedSourceIp={GOOD_IP}/32" in out  # a lone address becomes a /32
    assert "git archive" in out and "HEAD" in out  # only what is committed is shipped
    assert "remote-setup.sh" in out
    assert "0.0.0.0/0" not in out


def test_the_archive_sent_to_the_vm_is_only_what_git_tracks_so_no_env_file_can_leave(tripwire):
    path, _ = tripwire
    out = _run("deploy.sh", "--my-ip", GOOD_IP, "--dry-run", extra_path=path).stdout

    copies = [line for line in out.splitlines() if line.startswith("[dry-run] scp")]
    assert copies and all("hapi.dump" in line for line in copies)
    for line in copies:
        assert ".env" not in line  # the copy commands never name the local .env
    assert any(
        line.startswith("[dry-run] git archive") and line.endswith("HEAD")
        for line in out.splitlines()
    )  # what is shipped is a git archive of a commit


def test_a_deploy_needs_the_dump_to_exist_unless_it_is_a_dry_run(tripwire, tmp_path):
    path, calls = tripwire

    result = _run(
        "deploy.sh", "--my-ip", GOOD_IP, "--dump", str(tmp_path / "no.dump"), extra_path=path
    )

    assert result.returncode != 0
    assert "dump" in result.stderr.lower()
    assert not calls.exists()


def test_teardown_asks_for_the_group_name_and_deletes_nothing_on_a_wrong_answer(tripwire):
    path, calls = tripwire

    result = _run("teardown.sh", stdin="something else\n", extra_path=path)

    assert result.returncode != 0
    assert "delete" not in (calls.read_text() if calls.exists() else "")


def test_teardown_dry_run_shows_the_delete_command_without_running_it(tripwire):
    path, calls = tripwire

    result = _run("teardown.sh", "--dry-run", extra_path=path)

    assert result.returncode == 0, result.stderr
    assert "az group delete" in result.stdout
    assert not calls.exists()


# ---- the order things happen in on the machine


def test_the_database_is_restored_before_hapi_is_started():
    text = (AZURE / "remote-setup.sh").read_text()

    # HAPI creates its own tables the first time it starts; restoring over them would clash.
    assert text.index("pg_restore") < text.index("up --build")
    assert text.index("up -d postgres") < text.index("pg_restore")


def test_the_restore_is_skipped_when_the_data_is_already_there():
    text = (AZURE / "remote-setup.sh").read_text()

    # The restore runs only if HAPI's own table is missing, so running the script again on a
    # machine that already has data cannot overwrite it.
    assert re.search(
        r"if\s+!\s+docker compose exec.*?grep -q hfj_resource;\s*then", text, re.DOTALL
    )


def test_the_database_password_is_random_and_kept_private_on_the_machine():
    text = (AZURE / "remote-setup.sh").read_text()

    assert "openssl rand" in text
    assert text.count("chmod 600") >= 2  # the private copy, and the copy the stack reads
    assert not re.search(r"POSTGRES_PASSWORD=\s*hapi\b", text)  # never the default


def test_the_source_links_point_at_the_machines_own_address():
    text = (AZURE / "remote-setup.sh").read_text()

    assert "PUBLIC_FHIR_BASE_URL=http://${PUBLIC_IP}:8080/fhir" in text


def test_the_dump_is_portable_between_machines_and_compressed():
    text = (AZURE / "make-dump.sh").read_text()

    assert "-Fc" in text and "--no-owner" in text and "--no-privileges" in text
    assert "azure/dump" in text


def test_the_dump_is_never_committed():
    ignored = (ROOT / ".gitignore").read_text()

    assert "azure/dump/" in ignored

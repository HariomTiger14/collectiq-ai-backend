"""render.yaml pins for the staged sportscardspro pipeline.

These assert the Blueprint, which is only half the truth: Render's DASHBOARD
start command is what actually runs. #217 was exactly that gap -- render.yaml
carried a correct flag for days while the dashboard ran without it, five nights
of prices froze, and every run reported success. So a green test here means the
file is right, not that production is. The dashboard must be read by a human.
"""

import pathlib
import re
import unittest

import yaml

RENDER_YAML = pathlib.Path("render.yaml")

DOWNLOAD_JOB = "collectiq-sportscardspro-download"
INGEST_JOB = "collectiq-sportscardspro-ingest"
OLD_ROTATION_JOB = "packlox-tier3-sportscardspro-rotation-sit"
FIVE_CSV_JOB_COMMAND = "scripts.refresh_pricecharting_catalog"

# The five-CSV window. Sports must not run in it.
FIVE_CSV_HOURS = {14, 15}

# Services that already exist in Render, pinned EXACTLY as the dashboard has
# them. See RenderNameIsIdentityTest for why these must not be "tidied".
EXISTING_SERVICE_NAMES = frozenset({
    "collectiq-pricecharting-refresh-sit",
    "packlox-discover-pricecharting-sets-sit",
    "packlox-backfill-pricecharting-sets-sit",
    "packlox-refresh-tracked-catalog-items-sit",
    "packlox-refresh-small-sets-sit",
    "packlox-refresh-kicksdb-catalog-sit",
    "packlox-tier3-sportscardspro-rotation-sit",
    "packlox-refresh-completed-pricecharting-categories-sit",
    "packlox-fx-rates-refresh-sit",
    "packlox-batch-reprice-sit",
    "packlox-price-alerts-sit",
    "packlox-catalog-promote-scan-derived-sit",
    "packlox-purge-scheduled-deletions-sit",
    "packlox-match-portfolio-catalog-sit",
})



def _services() -> dict[str, dict]:
    doc = yaml.safe_load(RENDER_YAML.read_text())
    return {service["name"]: service for service in doc["services"]}


def _cron_hours(schedule: str) -> set[int]:
    """Expand the hour field of a 5-field cron expression."""
    hour_field = schedule.split()[1]
    hours: set[int] = set()
    for part in hour_field.split(","):
        if part == "*":
            return set(range(24))
        if "-" in part:
            lo, hi = part.split("-")
            hours |= set(range(int(lo), int(hi) + 1))
        else:
            hours.add(int(part))
    return hours


class StagedSportsCronTest(unittest.TestCase):
    def setUp(self) -> None:
        self.services = _services()

    def test_both_jobs_exist(self) -> None:
        for name in (DOWNLOAD_JOB, INGEST_JOB):
            self.assertIn(name, self.services)

    def test_the_download_start_command_is_pinned(self) -> None:
        self.assertEqual(
            self.services[DOWNLOAD_JOB]["startCommand"],
            "python -m scripts.download_catalog_batch --source sportscardspro "
            "--commit --max-queue-depth 1")

    def test_the_ingest_start_command_is_pinned(self) -> None:
        self.assertEqual(
            self.services[INGEST_JOB]["startCommand"],
            "python -m scripts.ingest_catalog_batch --source sportscardspro --commit")

    def test_the_download_runs_single_writer(self) -> None:
        """--max-queue-depth 1 is the whole backpressure story for this cron.

        Without it the argparse default of 2 applies, and at a 10-minute cadence
        2 means two concurrent disk writers: a ~7.5-minute ingest outlives the
        tick that fed it.
        """
        self.assertIn("--max-queue-depth 1",
                      self.services[DOWNLOAD_JOB]["startCommand"])

    def test_neither_job_writes_without_commit(self) -> None:
        for name in (DOWNLOAD_JOB, INGEST_JOB):
            with self.subTest(job=name):
                self.assertIn("--commit", self.services[name]["startCommand"])

    def test_only_the_downloader_gets_the_vendor_token(self) -> None:
        """The ingester reads a file already in storage and never contacts the
        vendor. Withholding the token is the guard that keeps it that way -- a
        stray vendor call from a job on a 10-minute schedule would blow the
        1-CSV-per-10-minutes account-wide budget."""
        def keys(name):
            return {var["key"] for var in self.services[name]["envVars"]}
        self.assertIn("PRICECHARTING_API_TOKEN", keys(DOWNLOAD_JOB))
        self.assertNotIn("PRICECHARTING_API_TOKEN", keys(INGEST_JOB))

    def test_both_jobs_have_the_supabase_secrets(self) -> None:
        for name in (DOWNLOAD_JOB, INGEST_JOB):
            with self.subTest(job=name):
                keys = {var["key"] for var in self.services[name]["envVars"]}
                self.assertIn("SUPABASE_URL", keys)
                self.assertIn("SUPABASE_SERVICE_ROLE_KEY", keys)

    def test_no_secret_value_is_committed(self) -> None:
        for name in (DOWNLOAD_JOB, INGEST_JOB):
            for var in self.services[name]["envVars"]:
                if var["key"] in ("PYTHON_VERSION", "ENVIRONMENT"):
                    continue
                with self.subTest(job=name, key=var["key"]):
                    self.assertIs(var.get("sync"), False)
                    self.assertNotIn("value", var)

    def test_both_jobs_avoid_the_five_csv_window(self) -> None:
        """The rejected proposal was "35 */2 * * *", which includes 14:35.

        Disk IOPS is a Supabase-side allowance not observable from inside
        Postgres, so contention with the five-CSV run surfaces as a slow night,
        not an error -- nothing would page anyone. The hole has to be in the
        schedule, because nothing downstream can detect its absence.
        """
        for name in (DOWNLOAD_JOB, INGEST_JOB):
            with self.subTest(job=name):
                hours = _cron_hours(self.services[name]["schedule"])
                self.assertEqual(hours & FIVE_CSV_HOURS, set(),
                                 f"{name} runs during the five-CSV window")
                self.assertEqual(hours, set(range(24)) - FIVE_CSV_HOURS)

    def test_the_ingest_cron_is_not_faster_than_an_ingest(self) -> None:
        """A 270k-row ingest measured 7m28s. On a */5 schedule the next tick
        starts while the first is still running."""
        schedule = self.services[INGEST_JOB]["schedule"]
        minutes = sorted(int(part) for part in schedule.split()[0].split(","))
        gaps = [b - a for a, b in zip(minutes, minutes[1:])]
        self.assertTrue(all(gap >= 10 for gap in gaps),
                        f"ingest ticks are {gaps} minutes apart; an ingest takes ~7.5")
        self.assertNotIn("*/5", schedule)

    def test_the_ingest_tick_follows_the_download_tick(self) -> None:
        """Offset so a file exists to pick up, rather than racing the download."""
        download = self.services[DOWNLOAD_JOB]["schedule"].split()[0]
        ingest_first = int(self.services[INGEST_JOB]["schedule"].split()[0].split(",")[0])
        self.assertEqual(download, "*/10")
        self.assertGreater(ingest_first, 0)
        self.assertLess(ingest_first, 10)

    def test_the_two_jobs_share_plan_region_and_branch(self) -> None:
        for field in ("plan", "region", "branch", "runtime"):
            with self.subTest(field=field):
                self.assertEqual(self.services[DOWNLOAD_JOB][field],
                                 self.services[INGEST_JOB][field])


class OldRotationUntouchedTest(unittest.TestCase):
    """The staged pair REPLACES the tier-3 rotation; it does not run beside it.

    Both claim from the same registry rotation, so running both would
    double-book the same sets and spend two vendor CSV slots on one batch.
    """

    def setUp(self) -> None:
        self.services = _services()

    def test_the_old_rotation_is_still_defined_and_unchanged(self) -> None:
        rotation = self.services[OLD_ROTATION_JOB]
        self.assertEqual(
            rotation["startCommand"],
            "python -m scripts.refresh_sportscardspro_rotation "
            "--max-requests 16 --batch-size 100")
        self.assertEqual(rotation["schedule"], "10 * * * *")

    def test_the_yaml_says_it_stays_suspended(self) -> None:
        """Suspension lives in the dashboard, which no test can read. The file
        can at least carry the reason, so the next person does not unsuspend it
        alongside the pair."""
        text = RENDER_YAML.read_text()
        preceding = text[:text.index(f"name: {OLD_ROTATION_JOB}")]
        self.assertIn("SUSPENDED", preceding[-500:])


class FiveCsvStartCommandUnchangedTest(unittest.TestCase):
    """Pinned because this PR touches the same file.

    #217: render.yaml carried the right flag while the dashboard did not, and
    five nights of prices froze behind five green runs.
    """

    def _five_csv_command(self) -> str:
        match = re.search(
            rf"startCommand: python -m {re.escape(FIVE_CSV_JOB_COMMAND)}[^\n]*",
            RENDER_YAML.read_text())
        self.assertIsNotNone(match, "five-CSV startCommand not found")
        return match.group(0)

    def test_it_still_passes_batch_size_900(self) -> None:
        self.assertIn("--batch-size 900", self._five_csv_command())

    def test_it_still_passes_use_storage(self) -> None:
        self.assertIn("--use-storage", self._five_csv_command())



class RenderNameIsIdentityTest(unittest.TestCase):
    """`name:` is a Blueprint's IDENTITY, not a label.

    Editing a name here does not rename the service in Render. It describes a
    service Render has never seen, so the Blueprint CREATES it and leaves the
    old one running -- two crons on the same schedule doing the same work. For
    collectiq-pricecharting-refresh-sit that is two five-CSV jobs writing the
    catalog nightly and two claims on a vendor budget of 144 CSVs a day.

    So the -sit suffix, ugly as it is, is pinned on every service that already
    exists. A real rename is a dashboard action first (Settings -> Name, which
    keeps the service id and its history), and only then this file, to match.
    Never this file first.

    The two sports crons are the exception only because Render has never seen
    them either way -- naming them without the suffix costs nothing.
    """

    def test_every_existing_service_keeps_its_exact_name(self) -> None:
        names = set(_services())
        missing = EXISTING_SERVICE_NAMES - names
        self.assertEqual(
            missing, set(),
            f"renamed in render.yaml without a dashboard rename first: {missing}. "
            f"The Blueprint will CREATE these as new services rather than rename "
            f"the running ones.")

    def test_the_two_new_sports_crons_carry_no_suffix(self) -> None:
        for name in (DOWNLOAD_JOB, INGEST_JOB):
            with self.subTest(service=name):
                self.assertIn(name, _services())
                self.assertFalse(name.endswith("-sit"))

    def test_no_service_is_both_suffixed_and_unsuffixed(self) -> None:
        """The shape a half-applied rename leaves behind: the old service still
        running and its duplicate alongside it."""
        names = set(_services())
        for name in names:
            with self.subTest(service=name):
                twin = name[:-4] if name.endswith("-sit") else f"{name}-sit"
                self.assertNotIn(twin, names,
                                 f"{name} and {twin} both defined -- that is a "
                                 f"duplicated service, not a rename")

    def test_the_custom_domain_was_not_renamed_with_the_services(self) -> None:
        """api-sit.packlox.com is DNS, not a service name. Six crons curl it and
        app/core/config.py defaults PUBLIC_API_URL to it, so a bulk
        find-and-replace over "-sit" would have silently pointed every one of
        them at a host that does not exist."""
        self.assertEqual(
            RENDER_YAML.read_text().count("https://api-sit.packlox.com"), 6)

    def test_the_environment_var_still_says_sit(self) -> None:
        """A rename does not promote anything to production.

        app/core/config.py reads ENVIRONMENT to decide
        subscription_allow_untrusted_sources, which in production rejects
        unverified subscription claims and fails closed when no store verifier
        is configured. That is a subscription decision, not a naming one.
        """
        declared = 0
        for name, service in _services().items():
            env = {var["key"]: var.get("value") for var in service["envVars"]}
            if "ENVIRONMENT" not in env:
                # The curl-based crons call the deployed API over HTTP; the var
                # lives on that service, not on them.
                continue
            declared += 1
            with self.subTest(service=name):
                self.assertEqual(env.get("ENVIRONMENT"), "sit")
        self.assertGreater(declared, 0, "no service declares ENVIRONMENT any more")


if __name__ == "__main__":
    unittest.main()

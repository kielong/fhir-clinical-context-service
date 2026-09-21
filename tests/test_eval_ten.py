# ORIGIN: AI — test cases typed by Claude Code from the agreed evaluation flow (fetch the packet,
#   GET every source it cites, compare), reviewed by Kiel. Cases marked (AI) are Claude Code's
#   additions, not yet adopted by Kiel.
"""The evaluation script: one patient in, one checked result out, and the run around it.

The service and HAPI are faked, so these tests are about what the script concludes, not about them.
"""

import json

import httpx
import pytest
import respx

from clinical_context.config import Settings
from eval_ten import evaluate, fetch_resource, fetch_total, main

API = "http://api.test"
FHIR = "http://fhir.test/fhir"
PATIENT_ID = "1000"
UUID = "2fa15bc7-8866-461a-9000-f739e425860a"
OTHER = "f7f63ca8-d282-4520-9a68-3177e2a5db6f"
PACKET_URL = f"{API}/v1/patients/{UUID}/clinical-context"
SUMMARY_TEXT = "The patient has recorded active anemia and a latex allergy."


def _summary(**overrides) -> dict:
    return {"status": "generated", "reason": None, "model": "m", "text": SUMMARY_TEXT, **overrides}


def _packet(*, summary=None, deceased=False, llm_ms=900) -> dict:
    return {
        "patient_id": UUID,
        "patient": {
            "fhir_id": PATIENT_ID,
            "source": f"Patient/{PATIENT_ID}",
            "age_years": 73,
            "gender": "male",
            "deceased": deceased,
        },
        "conditions": [
            {"display": "Anemia", "clinical_status": "active", "source": "Condition/c1"}
        ],
        "medications": [],
        "allergies": [
            {"display": "Latex", "clinical_status": "active", "source": "AllergyIntolerance/a1"}
        ],
        "summary": summary or _summary(),
        "missing": [],
        "meta": {
            "excluded_counts": {"conditions": 2, "medications": 1, "allergies": 0},
            "invalid_counts": {"conditions": 0, "medications": 0, "allergies": 0},
            "truncated": False,
            "timings_ms": {"fhir": 40, "llm": llm_ms, "total": 950},
        },
    }


PATIENT = {"resourceType": "Patient", "id": PATIENT_ID}
CONDITION = {
    "resourceType": "Condition",
    "id": "c1",
    "code": {"text": "Anemia"},
    "clinicalStatus": {"coding": [{"code": "active"}]},
    "subject": {"reference": f"Patient/{PATIENT_ID}"},
}
ALLERGY = {
    "resourceType": "AllergyIntolerance",
    "id": "a1",
    "code": {"text": "Latex"},
    "clinicalStatus": {"coding": [{"code": "active"}]},
    "patient": {"reference": f"Patient/{PATIENT_ID}"},
}
# What the packet above accounts for: 1 shown + 2 excluded, 0 + 1, 1 + 0.
HAPI_TOTALS = {"Condition": 3, "MedicationRequest": 1, "AllergyIntolerance": 1}


def _count_route(router, type_name: str, total: int):
    return router.get(
        f"{FHIR}/{type_name}", params={"patient": PATIENT_ID, "_summary": "count"}
    ).respond(200, json={"resourceType": "Bundle", "total": total})


@pytest.fixture
def servers():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(f"{FHIR}/Patient/{PATIENT_ID}").respond(200, json=PATIENT)
        router.get(f"{FHIR}/Condition/c1").respond(200, json=CONDITION)
        router.get(f"{FHIR}/AllergyIntolerance/a1").respond(200, json=ALLERGY)
        for type_name, total in HAPI_TOTALS.items():
            _count_route(router, type_name, total)
        yield router


def _run(*, patience_s: float = 0, pauses: list | None = None):
    pauses = pauses if pauses is not None else []
    with httpx.Client() as client:
        return evaluate(client, API, FHIR, UUID, patience_s=patience_s, pause=pauses.append)


# ---- fetching from HAPI


def test_a_source_that_exists_is_returned_as_the_record(servers):
    with httpx.Client() as client:
        assert fetch_resource(client, FHIR, "Condition/c1") == CONDITION


@pytest.mark.parametrize("status", [404, 410])
def test_a_source_that_is_gone_or_never_existed_is_none(servers, status):
    servers.get(f"{FHIR}/Condition/gone").respond(status, json={"resourceType": "OperationOutcome"})

    with httpx.Client() as client:
        assert fetch_resource(client, FHIR, "Condition/gone") is None


def test_a_server_error_is_an_error_not_an_unresolved_source(servers):
    # (AI) If HAPI itself is failing, calling every source "unresolved" would blame the packet.
    servers.get(f"{FHIR}/Condition/boom").respond(500)

    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        fetch_resource(client, FHIR, "Condition/boom")


def test_how_many_records_hapi_holds_for_a_patient_is_read_from_its_own_count(servers):
    with httpx.Client() as client:
        assert fetch_total(client, FHIR, "Condition", PATIENT_ID) == 3


def test_the_count_is_asked_for_fresh_because_hapi_reuses_search_results_for_a_minute(servers):
    with httpx.Client() as client:
        fetch_total(client, FHIR, "Condition", PATIENT_ID)

    request = servers.calls.last.request
    assert request.headers["Cache-Control"] == "no-cache"


# ---- one patient


def test_a_good_packet_is_fetched_and_every_source_is_checked(servers):
    servers.get(PACKET_URL).respond(200, json=_packet())

    result, packet = _run()

    assert packet["patient"]["fhir_id"] == PATIENT_ID
    assert (result.http_status, result.summary_status, result.summary_reason) == (
        200,
        "generated",
        None,
    )
    assert result.counts == (1, 0, 1)
    assert result.excluded == (2, 1, 0)
    assert [c.claim.source for c in result.checks] == [
        "Patient/1000",
        "Condition/c1",
        "AllergyIntolerance/a1",
    ]
    assert all(c.resolved and c.subject_ok for c in result.checks)
    assert [c.display_ok for c in result.checks] == [None, True, True]
    assert result.wall_ms >= 0
    assert result.text_violation is None
    assert result.problem is None
    assert result.flags == []


def test_the_packet_is_checked_for_completeness_against_hapis_own_counts(servers):
    servers.get(PACKET_URL).respond(200, json=_packet())

    result, _ = _run()

    assert [(a.section, a.hapi_total, a.accounted) for a in result.accounts] == [
        ("conditions", 3, 3),
        ("medications", 1, 1),
        ("allergies", 1, 1),
    ]


def test_a_packet_that_is_missing_records_hapi_holds_is_incomplete(servers):
    servers.get(PACKET_URL).respond(200, json=_packet())
    _count_route(servers, "Condition", 100)  # HAPI holds far more than the packet accounts for

    result, _ = _run()

    assert [a.ok for a in result.accounts] == [False, True, True]


def test_a_source_hapi_does_not_have_fails_the_check(servers):
    servers.get(PACKET_URL).respond(200, json=_packet())
    servers.get(f"{FHIR}/Condition/c1").respond(404, json={})

    result, _ = _run()

    missing = [c for c in result.checks if c.claim.source == "Condition/c1"]
    assert missing[0].resolved is False


def test_a_fact_whose_text_is_not_in_its_record_fails_the_display_check(servers):
    servers.get(PACKET_URL).respond(200, json=_packet())
    servers.get(f"{FHIR}/Condition/c1").respond(200, json={**CONDITION, "code": {"text": "Asthma"}})

    result, _ = _run()

    condition = [c for c in result.checks if c.claim.source == "Condition/c1"][0]
    assert (condition.resolved, condition.subject_ok, condition.display_ok) == (True, True, False)


def test_a_patient_the_api_cannot_find_has_no_packet_and_no_checks(servers):
    servers.get(PACKET_URL).respond(404, json={"detail": "Patient not found"})

    result, packet = _run()

    assert packet is None
    assert (result.http_status, result.summary_status, result.checks) == (404, None, [])


def test_an_api_that_is_not_running_is_reported_not_raised(servers):
    servers.get(PACKET_URL).mock(side_effect=httpx.ConnectError("refused"))

    result, packet = _run()

    assert packet is None
    assert result.http_status == 0


def test_a_hapi_failure_while_checking_is_recorded_as_a_problem_not_a_crash(servers):
    # (AI) One bad response must not throw away the rest of a long run.
    servers.get(PACKET_URL).respond(200, json=_packet())
    servers.get(f"{FHIR}/Condition/c1").respond(500)

    result, packet = _run()

    assert packet is not None
    assert result.problem is not None and "HTTP 500" in result.problem
    assert result.summary_status == "generated"  # what we did learn is kept


def test_a_hapi_that_stops_answering_is_a_problem_too(servers):
    servers.get(PACKET_URL).respond(200, json=_packet())
    servers.get(f"{FHIR}/Condition/c1").mock(side_effect=httpx.ConnectError("gone"))

    result, _ = _run()

    assert result.problem is not None and "ConnectError" in result.problem


# ---- waiting for a summary that timed out


TIMED_OUT = {"status": "unavailable", "reason": "timeout", "model": "m", "text": None}


def test_a_summary_that_timed_out_is_asked_for_again_until_it_is_ready(servers):
    # The service keeps generating after it stops waiting and remembers the answer, so asking
    # again a little later gets the finished summary.
    route = servers.get(PACKET_URL)
    route.side_effect = [
        httpx.Response(200, json=_packet(summary=TIMED_OUT)),
        httpx.Response(200, json=_packet(summary=TIMED_OUT)),
        httpx.Response(200, json=_packet(llm_ms=0)),  # remembered by now: took the service 0 ms
    ]
    pauses: list = []

    result, packet = _run(patience_s=60, pauses=pauses)

    assert route.call_count == 3
    assert pauses == [5, 5]
    assert result.summary_status == "generated"
    assert packet["summary"]["status"] == "generated"
    assert result.cached_before is False  # the wait was real, so its time counts


def test_it_gives_up_waiting_when_the_patience_runs_out(servers):
    route = servers.get(PACKET_URL).respond(200, json=_packet(summary=TIMED_OUT))

    result, _ = _run(patience_s=0)

    assert route.call_count == 1
    assert (result.summary_status, result.summary_reason) == ("unavailable", "timeout")


@pytest.mark.parametrize("reason", ["invalid_output", "policy_violation", "model_unreachable"])
def test_only_a_timeout_is_waited_for(servers, reason):
    # (AI) Any other failure would not be fixed by asking again a moment later.
    failed = {"status": "unavailable", "reason": reason, "model": "m", "text": None}
    route = servers.get(PACKET_URL).respond(200, json=_packet(summary=failed))

    _run(patience_s=60)

    assert route.call_count == 1


def test_a_summary_the_service_already_had_is_marked_so_its_speed_is_not_mistaken_for_the_models(
    servers,
):
    servers.get(PACKET_URL).respond(200, json=_packet(llm_ms=0))

    result, _ = _run()

    assert result.cached_before is True


def test_a_freshly_generated_summary_is_not_marked_as_remembered(servers):
    servers.get(PACKET_URL).respond(200, json=_packet(llm_ms=12000))

    result, _ = _run()

    assert result.cached_before is False


# ---- checking the words that came back


def test_summary_text_that_breaks_a_word_rule_is_noticed_even_though_the_service_should_refuse_it(
    servers,
):
    # (AI) An independent re-check of what was returned.
    bad = _summary(text="Approval is recommended.")
    servers.get(PACKET_URL).respond(200, json=_packet(summary=bad))

    result, _ = _run()

    assert result.text_violation == "determination_language"


def test_present_tense_treatment_is_flagged_for_a_person_to_read(servers):
    servers.get(PACKET_URL).respond(
        200, json=_packet(summary=_summary(text="The patient is taking warfarin."))
    )

    result, _ = _run()

    assert result.flags == ["present_tense"]


def test_a_deceased_patients_summary_that_never_says_so_is_flagged(servers):
    silent = _summary(text="A 73-year-old male had anemia and a latex allergy.")
    servers.get(PACKET_URL).respond(200, json=_packet(summary=silent, deceased=True))

    result, _ = _run()

    assert result.flags == ["deceased_not_stated"]


def test_a_long_chart_summarized_without_saying_there_are_more_is_flagged(servers):
    long_chart = _packet(summary=_summary(text="The patient has recorded active anemia."))
    long_chart["conditions"] = long_chart["conditions"] * 5  # five conditions in the packet
    servers.get(PACKET_URL).respond(200, json=long_chart)

    result, _ = _run()

    assert "partial_list" in result.flags


# ---- the whole run


def _serve(servers, *, uuid=UUID, packet=None):
    servers.get(f"{API}/v1/patients/{uuid}/clinical-context").respond(200, json=packet or _packet())


def _main(*argv):
    return main([*argv, "--api", API, "--fhir", FHIR, "--patience", "0"], settings=_settings())


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, fhir_base_url=FHIR, **overrides)


def test_a_clean_run_exits_zero(servers, capsys):
    _serve(servers)

    assert _main(UUID) == 0
    assert "sources: 3/3 resolved" in capsys.readouterr().out


def test_the_summary_text_is_hidden_unless_asked_for(servers, capsys):
    # This is what lets a person judge summaries only after the standard is fixed.
    _serve(servers)

    _main(UUID)

    assert SUMMARY_TEXT not in capsys.readouterr().out


def test_the_summary_text_is_shown_when_asked_for(servers, capsys):
    _serve(servers)

    _main(UUID, "--show-summary")

    assert SUMMARY_TEXT in capsys.readouterr().out


def test_a_source_that_does_not_check_out_makes_the_run_fail(servers):
    _serve(servers)
    servers.get(f"{FHIR}/Condition/c1").respond(404, json={})

    assert _main(UUID) == 1


def test_a_fact_whose_text_is_not_the_records_makes_the_run_fail(servers):
    _serve(servers)
    servers.get(f"{FHIR}/Condition/c1").respond(200, json={**CONDITION, "code": {"text": "Asthma"}})

    assert _main(UUID) == 1


def test_an_incomplete_packet_makes_the_run_fail(servers):
    _serve(servers)
    _count_route(servers, "Condition", 100)

    assert _main(UUID) == 1


def test_a_patient_with_no_packet_makes_the_run_fail_even_if_the_others_are_clean(servers):
    _serve(servers)
    servers.get(f"{API}/v1/patients/{OTHER}/clinical-context").respond(404, json={})

    assert _main(UUID, OTHER) == 1


def test_an_api_that_is_down_for_everyone_is_exit_two(servers):
    servers.get(PACKET_URL).mock(side_effect=httpx.ConnectError("refused"))

    assert _main(UUID) == 2


def test_one_hapi_failure_does_not_stop_the_other_patients_and_still_fails_the_run(servers, capsys):
    _serve(servers)
    _serve(servers, uuid=OTHER)
    # The first patient's condition fails once with a 500; the second patient is the same shape.
    servers.get(f"{FHIR}/Condition/c1").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=CONDITION)]
    )

    exit_code = _main(UUID, OTHER)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.count("==== ") == 2  # both patients were evaluated
    assert "could not finish" in out


def test_the_ids_can_come_from_a_file_with_comments(servers, tmp_path, capsys):
    ids = tmp_path / "ids.txt"
    ids.write_text(f"# the ten\n{UUID}  # Aaron\n")
    _serve(servers)

    assert _main("--file", str(ids)) == 0
    assert "==== 1/1" in capsys.readouterr().out


def test_results_can_be_saved_one_line_per_patient_so_a_long_run_is_never_lost(servers, tmp_path):
    _serve(servers)
    _serve(servers, uuid=OTHER)
    out = tmp_path / "results.jsonl"

    _main(UUID, OTHER, "--out", str(out))

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["uuid"] for row in rows] == [UUID, OTHER]
    assert rows[0]["summary_status"] == "generated"
    assert rows[0]["counts"] == [1, 0, 1]
    assert "text" not in rows[0] and SUMMARY_TEXT not in out.read_text()  # facts, never the words


# ---- the batch


def _bundle_dir(tmp_path, count: int):
    for i in range(count):
        name = f"Name{i}_Surname{i}_{i:08x}-0000-0000-0000-000000000000.json"
        (tmp_path / name).write_text("{}")
    return tmp_path


def test_listing_a_sample_prints_the_ids_and_calls_nothing(servers, tmp_path, capsys):
    settings = _settings(seed_data_dir=str(_bundle_dir(tmp_path, 12)))

    code = main(["--batch", "3", "--seed", "1", "--list", "--api", API], settings=settings)

    out = capsys.readouterr().out.split()
    assert code == 0
    assert len([word for word in out if word.endswith("0000-0000-0000-000000000000")]) == 3
    assert servers.calls.call_count == 0  # no API, no HAPI


def test_a_batch_samples_evaluates_and_reports_rates(servers, tmp_path, capsys):
    data = _bundle_dir(tmp_path, 3)
    ids = sorted(f"{i:08x}-0000-0000-0000-000000000000" for i in range(3))
    for uuid in ids:
        _serve(servers, uuid=uuid)
    settings = _settings(seed_data_dir=str(data))

    code = main(
        ["--batch", "3", "--seed", "1", "--api", API, "--fhir", FHIR, "--patience", "0"],
        settings=settings,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "patients: 3" in out
    assert "schema-valid: 3/3" in out
    assert "sources: resolved 9/9" in out
    assert SUMMARY_TEXT not in out


def test_stopping_a_long_run_with_ctrl_c_still_reports_and_saves_what_finished(
    servers, tmp_path, monkeypatch, capsys
):
    import eval_ten

    real = eval_ten.evaluate
    calls = []

    def second_one_is_interrupted(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(eval_ten, "evaluate", second_one_is_interrupted)
    _serve(servers)
    out = tmp_path / "partial.jsonl"

    code = _main(UUID, OTHER, "--out", str(out))

    assert code == 130
    assert len(out.read_text().splitlines()) == 1  # the first patient was already saved
    assert "totals: 1 patients" in capsys.readouterr().out

import json

from gateway.todo_progress import TodoChecklist


def _result(todos):
    return json.dumps({"todos": todos, "summary": {}})


def test_authoritative_full_result_replaces_state():
    checklist = TodoChecklist()

    first = checklist.update_from_result(_result([
        {"id": "inspect", "content": "Inspect configuration", "status": "in_progress"},
        {"id": "verify", "content": "Run tests", "status": "pending"},
    ]))
    assert first == (
        "Working on 2 tasks:\n\n"
        "🔄 Inspect configuration\n"
        "⬜ Run tests"
    )

    updated = checklist.update_from_result(_result([
        {"id": "inspect", "content": "Inspect configuration", "status": "completed"},
        {"id": "verify", "content": "Run tests", "status": "in_progress"},
        {"id": "ship", "content": "Ship change", "status": "pending"},
    ]))
    assert updated == (
        "Working on 2 tasks:\n\n"
        "🔄 Run tests\n"
        "⬜ Ship change\n"
        "✅ Inspect configuration"
    )


def test_empty_authoritative_result_clears_visible_tasks():
    checklist = TodoChecklist()
    checklist.update_from_result(_result([
        {"id": "task", "content": "Task", "status": "pending"},
    ]))

    assert checklist.update_from_result(_result([])) == ""
    assert checklist.render() is None


def test_malformed_or_error_result_does_not_replace_last_valid_state():
    checklist = TodoChecklist()
    valid = checklist.update_from_result(_result([
        {"id": "task", "content": "Task", "status": "pending"},
    ]))

    assert checklist.update_from_result("not json") is None
    assert checklist.update_from_result({"error": "failed"}) is None
    assert checklist.render() == valid


def test_delegated_goals_render_below_main_tasks():
    checklist = TodoChecklist(delegated_tasks="goal")
    checklist.update_from_result(_result([
        {"id": "main", "content": "Main task", "status": "in_progress"},
    ]))

    rendered = checklist.add_delegations({
        "tasks": [
            {"goal": "Review implementation"},
            {"goal": "Verify rendering"},
        ]
    })

    assert rendered == (
        "Working on 1 task:\n\n"
        "🔄 Main task\n\n"
        "🤖 Delegated tasks\n"
        "↳ Review implementation\n"
        "↳ Verify rendering"
    )


def test_single_delegated_goal_is_rendered():
    checklist = TodoChecklist(delegated_tasks="goal")

    rendered = checklist.add_delegations({"goal": "Audit the implementation"})

    assert rendered == (
        "🤖 Delegated tasks\n"
        "↳ Audit the implementation"
    )


def test_delegated_goals_are_deduplicated_across_retries():
    checklist = TodoChecklist(delegated_tasks="goal")

    checklist.add_delegations({"goal": "Review gateway integration"})
    checklist.add_delegations({"goal": "Review gateway integration"})

    rendered = checklist.render()
    assert rendered is not None
    assert rendered.count("↳ Review gateway integration") == 1


def test_delegated_tasks_default_off():
    checklist = TodoChecklist()

    assert checklist.add_delegations({"goal": "Review gateway integration"}) is None
    assert checklist.render() is None


def test_delegated_task_count_mode():
    checklist = TodoChecklist(delegated_tasks="count")

    rendered = checklist.add_delegations({
        "tasks": [
            {"goal": "Review implementation"},
            {"goal": "Verify rendering"},
        ]
    })

    assert rendered == "Delegated 2 tasks 🤖"


def test_delegated_task_count_mode_uses_singular():
    checklist = TodoChecklist(delegated_tasks="count")

    assert checklist.add_delegations({"goal": "Review implementation"}) == (
        "Delegated 1 task 🤖"
    )


def test_delegated_goal_mode_caps_each_goal_at_80_characters():
    checklist = TodoChecklist(delegated_tasks="goal")
    long_goal = "x" * 100

    rendered = checklist.add_delegations({"goal": long_goal})

    assert rendered is not None
    displayed_goal = rendered.split("↳ ", 1)[1]
    assert len(displayed_goal) == 80
    assert displayed_goal == "x" * 79 + "…"


def test_invalid_authoritative_item_is_ignored_as_a_whole_update():
    checklist = TodoChecklist()

    assert checklist.update_from_result(_result([
        {"id": "bad", "content": "Unknown status", "status": "blocked"},
    ])) is None
    assert checklist.render() is None

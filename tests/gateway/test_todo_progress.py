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
        "📋 Tasks — 0/2 completed\n\n"
        "🔄 Inspect configuration\n"
        "⬜ Run tests"
    )

    updated = checklist.update_from_result(_result([
        {"id": "inspect", "content": "Inspect configuration", "status": "completed"},
        {"id": "verify", "content": "Run tests", "status": "in_progress"},
        {"id": "ship", "content": "Ship change", "status": "pending"},
    ]))
    assert updated == (
        "📋 Tasks — 1/3 completed\n\n"
        "✅ Inspect configuration\n"
        "🔄 Run tests\n"
        "⬜ Ship change"
    )


def test_empty_authoritative_result_clears_visible_tasks():
    checklist = TodoChecklist()
    checklist.update_from_result(_result([
        {"id": "task", "content": "Task", "status": "pending"},
    ]))

    assert checklist.update_from_result(_result([])) == (
        "📋 Tasks — 0/0 completed\n\nNo tasks"
    )


def test_malformed_or_error_result_does_not_replace_last_valid_state():
    checklist = TodoChecklist()
    valid = checklist.update_from_result(_result([
        {"id": "task", "content": "Task", "status": "pending"},
    ]))

    assert checklist.update_from_result("not json") is None
    assert checklist.update_from_result({"error": "failed"}) is None
    assert checklist.render() == valid


def test_delegated_goals_render_below_main_tasks():
    checklist = TodoChecklist()
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
        "📋 Tasks — 0/1 completed\n\n"
        "🔄 Main task\n\n"
        "🤖 Delegated tasks\n"
        "↳ Review implementation\n"
        "↳ Verify rendering"
    )


def test_single_delegated_goal_is_rendered():
    checklist = TodoChecklist()

    rendered = checklist.add_delegations({"goal": "Audit the implementation"})

    assert rendered == (
        "📋 Tasks — 0/0 completed\n\n"
        "🤖 Delegated tasks\n"
        "↳ Audit the implementation"
    )


def test_delegated_goals_are_deduplicated_across_retries():
    checklist = TodoChecklist()

    checklist.add_delegations({"goal": "Review gateway integration"})
    checklist.add_delegations({"goal": "Review gateway integration"})

    rendered = checklist.render()
    assert rendered is not None
    assert rendered.count("↳ Review gateway integration") == 1


def test_invalid_authoritative_item_is_ignored_as_a_whole_update():
    checklist = TodoChecklist()

    assert checklist.update_from_result(_result([
        {"id": "bad", "content": "Unknown status", "status": "blocked"},
    ])) is None
    assert checklist.render() is None

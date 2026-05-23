import frappe
from frappe.tests.utils import FrappeTestCase

from astra import api, fac_bridge


class TestAstraSecurity(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.user_a = self._make_user("astra.user.a@example.com")
        self.user_b = self._make_user("astra.user.b@example.com")

    def tearDown(self):
        frappe.set_user("Administrator")

    def test_guest_cannot_use_astra(self):
        frappe.set_user("Guest")
        with self.assertRaises(frappe.ValidationError):
            api.get_chat_history()

    def test_user_cannot_read_another_users_session(self):
        frappe.set_user(self.user_a)
        session_id = api._get_or_create_session(None, "hello")

        frappe.set_user(self.user_b)
        with self.assertRaises(frappe.ValidationError):
            api.get_chat_history(session_id)

    def test_confirmation_owner_required(self):
        frappe.set_user(self.user_a)
        session_id = api._get_or_create_session(None, "create something")
        confirmation_id = api._create_tool_confirmation(
            session_id=session_id,
            tool_call={"name": "create_document", "arguments": {"doctype": "ToDo"}},
            requested_message="request",
        )

        frappe.set_user(self.user_b)
        with self.assertRaises(frappe.ValidationError):
            api.resolve_tool_confirmation(confirmation_id, "reject")

    def test_write_tools_require_confirmation(self):
        result = fac_bridge.execute_tool(
            {"name": "create_document", "arguments": {"doctype": "ToDo"}},
            allowlist={"create_document"},
        )
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "confirmation_required")

    def test_confirmation_creates_tool_event(self):
        if not frappe.db.exists("DocType", "Astra Tool Event"):
            return
        frappe.set_user(self.user_a)
        session_id = api._get_or_create_session(None, "create something")
        confirmation_id = api._create_tool_confirmation(
            session_id=session_id,
            tool_call={"name": "create_document", "arguments": {"doctype": "ToDo", "fields": {"description": "safe"}}},
            requested_message="request",
        )
        self.assertTrue(confirmation_id)
        self.assertTrue(frappe.db.exists("Astra Tool Event", {"session": session_id, "event_type": "Confirmation"}))

    def test_sensitive_preview_is_redacted(self):
        preview = fac_bridge.build_write_preview(
            "create_document",
            {"doctype": "ToDo", "fields": {"api_key": "secret", "description": "visible"}},
        )
        changes = {row["fieldname"]: row["after"] for row in preview.get("changes") or []}
        self.assertEqual(changes.get("api_key"), "***")
        self.assertEqual(changes.get("description"), "visible")

    def test_always_blocked_tools_remain_blocked(self):
        allowlist = fac_bridge.parse_allowlist(
            "run_python_code\nrun_database_query\nget_document",
            include_confirmation_tools=True,
        )
        self.assertIn("get_document", allowlist)
        self.assertNotIn("run_python_code", allowlist)
        self.assertNotIn("run_database_query", allowlist)

    def _make_user(self, email):
        if frappe.db.exists("User", email):
            return email

        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": email.split("@")[0],
                "enabled": 1,
                "send_welcome_email": 0,
            }
        )
        user.insert(ignore_permissions=True)
        return email

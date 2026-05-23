app_name = "astra"
app_title = "Astra"
app_publisher = "Local"
app_description = "A local, permission-aware AI chat assistant for Frappe Desk."
app_email = "admin@example.com"
app_license = "MIT"

app_include_js = "/assets/astra/js/assistant_chat.js"
app_include_css = "/assets/astra/css/assistant_chat.css"

after_install = "astra.install.after_install"

doctype_js = {}

scheduler_events = {
    "daily": ["astra.jobs.daily_maintenance"],
}

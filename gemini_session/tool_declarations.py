"""Function declarations passed to LiveConnectConfig(tools=...). Names here
must match tools/registry.py's dispatch table exactly.
"""

FUNCTION_DECLARATIONS = [
    {
        "name": "list_services",
        "description": "List all nail services CLASSY NAIL SPA offers, with price and duration.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "quote_price",
        "description": "Get the price and duration for one named service.",
        "parameters": {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
            "required": ["service_name"],
        },
    },
    {
        "name": "check_availability",
        "description": "Check open appointment time slots for a service on a given date at one branch.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Exact service name from list_services"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "branch": {"type": "string", "description": "The branch the caller chose (from the branch list)"},
            },
            "required": ["service_name", "date", "branch"],
        },
    },
    {
        "name": "create_booking",
        "description": (
            "Record a new appointment REQUEST (not a confirmation): the slot is held and the team confirms it "
            "later. Only after review_booking, the read-back and the caller's clear yes. Never tell the caller "
            "it is booked or confirmed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Exact service name from list_services"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time": {"type": "string", "description": "Time in HH:MM 24-hour format, one of the times check_availability returned"},
                "branch": {"type": "string", "description": "The branch the caller chose"},
                "customer_name": {"type": "string", "description": "The name the caller confirmed when you repeated it back"},
                "customer_phone": {
                    "type": "string",
                    "description": (
                        "The phone number the caller confirmed (read back digit by digit). If they chose the "
                        "number they are calling from, pass that number."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "Design idea, gel or acrylic, other services wanted the same day, etc.",
                },
            },
            "required": ["service_name", "date", "time", "branch", "customer_name", "customer_phone"],
        },
    },
    {
        "name": "review_booking",
        "description": (
            "ALWAYS call this before create_booking (or update_booking): it checks the details and returns them in "
            "the form to read back. Read them back, ask if everything is correct and WAIT for the caller's answer. "
            "Nothing is saved by this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Exact service name from list_services"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time": {"type": "string", "description": "Time in HH:MM 24-hour format, one of the times check_availability returned"},
                "branch": {"type": "string", "description": "The branch the caller chose"},
                "customer_name": {"type": "string", "description": "The name the caller confirmed when you repeated it back"},
                "customer_phone": {
                    "type": "string",
                    "description": "The full 10-digit phone number the caller confirmed (read back digit by digit).",
                },
                "notes": {"type": "string", "description": "Design idea, gel or acrylic, other services the same day"},
                "booking_id": {"type": "string", "description": "Only when correcting a booking already saved in this call"},
            },
            "required": ["service_name", "date", "time", "branch", "customer_name", "customer_phone"],
        },
    },
    {
        "name": "update_booking",
        "description": (
            "Correct a booking request you already saved in THIS call (wrong name, number, branch, service, date or "
            "time), after review_booking with its booking_id and the caller's yes. Pass the full corrected details."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Exact service name from list_services"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time": {"type": "string", "description": "Time in HH:MM 24-hour format, one of the times check_availability returned"},
                "branch": {"type": "string", "description": "The branch the caller chose"},
                "customer_name": {"type": "string", "description": "The name the caller confirmed when you repeated it back"},
                "customer_phone": {
                    "type": "string",
                    "description": "The full 10-digit phone number the caller confirmed (read back digit by digit).",
                },
                "notes": {"type": "string", "description": "Design idea, gel or acrylic, other services the same day"},
                "booking_id": {"type": "string", "description": "Only when correcting a booking already saved in this call"},
            },
            "required": ["booking_id", "service_name", "date", "time", "branch", "customer_name", "customer_phone"],
        },
    },
    {
        "name": "find_booking",
        "description": (
            "Find the caller's existing active booking (e.g. before a reschedule) by the name it was made under, "
            "phone number and/or date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "customer_phone": {"type": "string"},
                "date": {"type": "string", "description": "The booking's current date, YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "modify_booking",
        "description": (
            "Request a new date and/or time for an existing booking. This is a REQUEST: the old appointment stays "
            "until the team confirms the change."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string"},
                "new_date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "new_time": {"type": "string", "description": "Time in HH:MM 24-hour format"},
            },
            "required": ["booking_id"],
        },
    },
    {
        "name": "cancel_booking",
        "description": "Cancel an existing booking.",
        "parameters": {
            "type": "object",
            "properties": {"booking_id": {"type": "string"}},
            "required": ["booking_id"],
        },
    },
    {
        "name": "take_message",
        "description": (
            "Record an inquiry, complaint or message for the team when the request can't be handled live "
            "(price inquiry, treatment question, complaint, location link by WhatsApp, callback). Complaints are "
            "always flagged urgent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "caller_phone": {"type": "string"},
                "message": {"type": "string", "description": "What the caller needs, with the details they gave"},
                "category": {
                    "type": "string",
                    "enum": ["general", "complaint", "price_inquiry", "treatment_inquiry", "location_link", "callback"],
                },
                "branch": {"type": "string"},
                "urgent": {"type": "boolean"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "transfer_call",
        "description": "Transfer the live call to a staff member, e.g. when the caller insists on a human.",
        "parameters": {
            "type": "object",
            "properties": {
                "target_number": {"type": "string", "description": "Staff phone number or extension to transfer to"},
                "reason": {"type": "string"},
            },
            "required": ["target_number"],
        },
    },
    {
        "name": "end_call",
        "description": "End the call, e.g. after the caller says goodbye or the conversation is complete.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
    },
]

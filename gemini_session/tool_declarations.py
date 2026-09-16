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
        "description": "Check open appointment time slots for a service on a given date.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
            },
            "required": ["service_name", "date"],
        },
    },
    {
        "name": "create_booking",
        "description": "Create a new confirmed appointment booking.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time": {"type": "string", "description": "Time in HH:MM 24-hour format"},
                "customer_name": {"type": "string"},
                "customer_phone": {"type": "string"},
            },
            "required": ["service_name", "date", "time", "customer_name", "customer_phone"],
        },
    },
    {
        "name": "modify_booking",
        "description": "Change the date and/or time of an existing booking.",
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
            "Record a message from the caller when their request can't be handled live; "
            "flags urgent ones for callback priority."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "caller_phone": {"type": "string"},
                "message": {"type": "string"},
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

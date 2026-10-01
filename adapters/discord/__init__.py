"""Interactive MCS card delivery for the Discord gateway.

Worker side of the D1 pipeline: claims render specs from
``data/discord_render``, asks the runner for a durable send grant
(``transport_begin``), delivers through the live discord.py client,
journals every phase transition, and settles each attempt with a
factual ``transport_receipt``. Component interactions route through a
single ``on_interaction`` listener — views and modals carry no business
logic of their own.

Every discord.py import is deferred into the functions that need it so
the plugin still registers ``/mcs`` on hosts without the messaging SDK.
"""

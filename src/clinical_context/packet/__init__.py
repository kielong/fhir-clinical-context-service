# ORIGIN: AI — package marker typed by Claude Code, reviewed by Kiel.
"""The packet: its shape (models) and the deterministic rules that build it from FHIR records.

Nothing here touches the network, a model or the clock, so the same records always give the same
packet. The FHIR client (fhir/) fetches the records and the language model (llm/) writes only the
summary; this package is what sits between them.
"""

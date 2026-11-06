"""Optional v1 receipts participate in scope and revision without changing legacy hashes."""

BOUNDARY_RECEIPTS = ("hypothesis_answer", "hypothesis_support", "image_evidence",
                     "pursuit_evidence", "catchphrase_evidence", "gap_answer")


def receipt_fields(metadata):
    return {key: metadata[key] for key in BOUNDARY_RECEIPTS
            if isinstance(metadata.get(key), dict) and metadata[key].get("version") == 1}

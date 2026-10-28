"""Physical operands for proof/IO tests that construct their own write plans.

The production selector supplies the witness; tests still choose their own
operation/fields and inject faults at the proof/IO boundary being exercised.
"""
import nexus_map_codebase as nmc


def physical_operand(path, language, bid):
    snapshot = nmc.read_source_snapshot(path)
    target = {"language": language, "note_beacon_id": bid, "operation": "delta_ensure"}
    selection = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, language), target)
    return {"selection_target": target, "physical_witness": selection["physical_witness"]}

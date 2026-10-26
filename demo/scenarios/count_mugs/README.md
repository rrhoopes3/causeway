# count_mugs

**Task:** "How many mugs are on the desk in desk.jpg? Write the count to count.txt."

**Planted:** `sandbox/desk.jpg` is a synthetic picture drawn with Pillow, not a photo. It shows four mugs (red, blue, white, green) and one yellow bowl that overlaps the white mug. The bowl is a distractor. The correct count is 4. SAM 3.1 may miscount with synthetic shapes. If it does, the run is still a valid example.

**Expected run:**
- The agent calls `segment_image` with `path` set to `desk.jpg` and `prompt` set to `mug`. The result is a summary such as `4 x "mug": boxes [[...], ...]`.
- The agent then calls `write_file` for `count.txt`. Its `path` traces to the task (`GOAL`).
- Causeway tags the count in `content` as `LIT`, not `IN`. A one-digit string is a trivial literal under SPEC 5.1 (length under 3), so it is never traced. The link from the count to the SAM result is therefore not visible in the ledger. This is a real gap in the spec. Check the count against the `segment_image` result by hand.

**Causeway tags to look for:** the `segment_image` call's `prompt` (`GOAL`), the `path` (`GOAL`), and the `write_file` `content` (`LIT`, per the note above). The tool result event for `segment_image` is the evidence.

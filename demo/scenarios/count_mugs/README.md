# count_mugs

**Task:** "Find every mug on the desk in desk.jpg. Write one line per mug to mugs.txt with its bounding box as x1,y1,x2,y2, then tell me how many there are."

**Planted:** `sandbox/desk.jpg` is a synthetic picture drawn with Pillow, not a photo. It shows four mugs (red, blue, white, green) and one yellow bowl that overlaps the white mug. The bowl is a distractor, and the correct count is 4. SAM 3.1 may miscount synthetic shapes. If it does, the run is still a valid example, and arguably a better one.

**Expected run:**
- The agent calls `segment_image` with `path` `desk.jpg` and a prompt such as `mug`. Both trace to the task (`GOAL`).
- It then calls `write_file` for `mugs.txt`. The `content` is several lines of numbers, so it is a prose leaf (SPEC §5.4c). Every coordinate of 2 or more digits is an atom.

**Causeway tags to look for:** each coordinate atom in `write_file.content` should trace `IN` (or `CARRY·k`) to the `segment_image` result event, which proves the numbers came from SAM 3.1 and not from the model. A coordinate tagged `UNSOURCED` was made up or mis-copied, and `show` names it.

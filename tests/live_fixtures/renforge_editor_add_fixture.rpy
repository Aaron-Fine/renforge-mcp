# Live fixture for literal add / decorative frame scene-walk adapter.

default renforge_add_expr_x = 80


screen renforge_editor_add_fixture():
    layer "screens"
    zorder 640

    add Solid("#0a0a12", xysize=(1280, 720))

    add Solid("#4f46e5", xysize=(160, 100)) id "add_target" xpos 200 ypos 180

    frame id "deco_frame" xpos 40 ypos 40 xysize (120, 80) background Solid("#22c55e")

    add Solid("#f59e0b", xysize=(80, 80)) id "add_expr" xpos renforge_add_expr_x ypos 400

    add Solid("#ef4444", xysize=(140, 100)) id "add_overlap_a" xpos 720 ypos 200
    add Solid("#3b82f6", xysize=(140, 100)) id "add_overlap_b" xpos 760 ypos 230

    add Transform(Solid("#a855f7", xysize=(100, 80)), rotate=15) id "add_transform" xpos 200 ypos 420

    add SideImage() id "add_sideimage" xpos 20 ypos 560

    textbutton "FOCUS" id "add_focus" xpos 980 ypos 40 action NullAction()

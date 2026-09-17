default rf_route_choice = "unset"
default rf_button_clicked = False
default rf_checkpoint_marker = 0

label start:
    scene expression Solid("#243447")
    "Synthetic strict-play fixture."
    menu:
        "Choose the verified route."
        "Continue safely":
            $ rf_route_choice = "safe"
        "Unsupported input branch":
            call screen rf_unsupported_input
    "The route choice was [rf_route_choice]."
    call screen rf_image_decision
    $ rf_checkpoint_marker = 42
    "Checkpoint marker [rf_checkpoint_marker]."
    "Identical line."
    "Identical line."
    "Fixture complete."
    return

screen rf_image_decision():
    modal True
    add Solid("#284b63")
    textbutton "Confirm image decision":
        id "rf_confirm"
        align (0.5, 0.5)
        action [SetVariable("rf_button_clicked", True), Return()]

screen rf_unsupported_input():
    modal True
    frame:
        align (0.5, 0.5)
        vbox:
            text "Unsupported text input branch"
            input value VariableInputValue("rf_route_choice")

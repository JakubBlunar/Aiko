// Prevents an extra terminal window from popping up alongside the GUI on
// Windows release builds. Has no effect on dev or other platforms.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    aiko_desktop_lib::run()
}

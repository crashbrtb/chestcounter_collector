//! The configuration interface, without a console window behind it.
#![windows_subsystem = "windows"]

fn main() {
    tbc::logger::set_console(false);
    let config = match tbc::cli::load_configuration() {
        Ok(c) => c,
        Err(e) => {
            tbc::gui::fatal("Total Battle Chest Collector", &e);
            std::process::exit(tbc::cli::EXIT_MISCONFIGURED);
        }
    };
    tbc::cli::configure_logging(&config);
    std::process::exit(tbc::gui::launch(config));
}

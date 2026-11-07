//! The collector for the Windows Task Scheduler (and the command line).

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match tbc::cli::parse(&args) {
        Ok(a) => tbc::cli::main_with(a),
        Err(e) => {
            eprintln!("{}\n\n{}", e, tbc::cli::USAGE);
            tbc::cli::EXIT_MISCONFIGURED
        }
    };
    std::process::exit(code);
}

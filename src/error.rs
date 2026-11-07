//! The ways a run can go wrong, told apart the way the run needs to react to them.
//!
//! A cancellation passes through everything; a session error loses a profile, a
//! person-only problem or a window that is not ours loses the account without a
//! restart; a game-link error is caught by whoever asked the game.

use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub enum Error {
    /// ESC held, or Stop pressed: raised at the first safe point.
    Cancelled(String),
    /// A login, a profile switch or a page that did not do what it should.
    Session(String),
    /// Only someone at the keyboard can get past this (the e-mailed code).
    NeedsPerson(String),
    /// The port or the folder belongs to another Chrome.
    WrongWindow(String),
    /// The browser could not be started, reached or talked to.
    Browser(String),
    /// The game could not be asked, or refused. Never carries the request itself.
    GameLink(String),
    /// The configuration does not let the run start.
    Runner(String),
    /// Another run is driving the browser right now.
    RunAlreadyActive(String),
    /// Anything else.
    Other(String),
}

impl Error {
    pub fn is_cancelled(&self) -> bool {
        matches!(self, Error::Cancelled(_))
    }

    /// SessionError in the original: NeedsPerson is one too.
    pub fn is_session(&self) -> bool {
        matches!(self, Error::Session(_) | Error::NeedsPerson(_))
    }

    pub fn is_runner(&self) -> bool {
        matches!(self, Error::Runner(_) | Error::RunAlreadyActive(_))
    }

    pub fn message(&self) -> &str {
        match self {
            Error::Cancelled(m)
            | Error::Session(m)
            | Error::NeedsPerson(m)
            | Error::WrongWindow(m)
            | Error::Browser(m)
            | Error::GameLink(m)
            | Error::Runner(m)
            | Error::RunAlreadyActive(m)
            | Error::Other(m) => m,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = self.message();
        if text.is_empty() {
            write!(f, "{:?}", self)
        } else {
            f.write_str(text)
        }
    }
}

impl std::error::Error for Error {}

pub type Res<T> = Result<T, Error>;

pub fn game(text: impl Into<String>) -> Error {
    Error::GameLink(text.into())
}

pub fn other(text: impl Into<String>) -> Error {
    Error::Other(text.into())
}

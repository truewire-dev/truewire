use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

/// Why `truewire mock` could not be started.
#[derive(Debug)]
pub enum MockError {
    /// The binary could not be run at all.
    Spawn { binary: PathBuf, source: std::io::Error },
    /// It exited, or went quiet for too long, before announcing its URLs.
    NotReady { binary: PathBuf, output: String },
}

impl std::fmt::Display for MockError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Spawn { binary, source } => write!(f, "could not run {}: {source}", binary.display()),
            Self::NotReady { binary, output } => {
                write!(f, "{} exited before announcing its URLs:\n{output}", binary.display())
            }
        }
    }
}

impl std::error::Error for MockError {}

/// `truewire mock` for one project on free ports, as a child process that is killed when
/// this value is dropped.
///
/// The binary is `TRUEWIRE_BIN` when set, else the nearest `.venv/bin/truewire` above the
/// project (a checkout's own environment), else `truewire` on `PATH`.
#[derive(Debug)]
pub struct Mock {
    child: Child,
    /// The HTTP base URL the mock serves recorded exchanges on.
    pub http_url: String,
    /// The WebSocket URL, or `None` when the project records no WebSocket example.
    pub ws_url: Option<String>,
}

impl Mock {
    /// Start the mock for the project rooted at `project` and wait until it has announced
    /// where it listens (30 seconds at most).
    pub fn start(project: impl AsRef<Path>) -> Result<Self, MockError> {
        let project = project.as_ref();
        let binary = Self::binary(project);
        let mut child = Command::new(&binary)
            .args(["mock", "--project"])
            .arg(project)
            .args(["--http-port", "0", "--ws-port", "0"])
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|source| MockError::Spawn {
                binary: binary.clone(),
                source,
            })?;
        let stdout = child.stdout.take().expect("stdout is piped");
        let (tx, rx) = mpsc::channel();
        // One thread reads every line for the mock's whole life, so it never blocks on a
        // full pipe; the first lines are handed over until both URLs are known.
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if tx.send(line).is_err() {
                    // Nobody is listening any more: keep draining.
                    continue;
                }
            }
        });
        let mut http_url = None;
        let mut seen = String::new();
        loop {
            match rx.recv_timeout(Duration::from_secs(30)) {
                Ok(line) => {
                    seen.push_str(&line);
                    seen.push('\n');
                    if let Some(url) = line.strip_prefix("HTTP") {
                        http_url = Some(url.trim().to_string());
                    } else if let Some(rest) = line.strip_prefix("WS") {
                        let rest = rest.trim();
                        let ws_url = (!rest.starts_with('(')).then(|| rest.to_string());
                        if let Some(http_url) = http_url {
                            return Ok(Self {
                                child,
                                http_url,
                                ws_url,
                            });
                        }
                    }
                }
                Err(_) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(MockError::NotReady { binary, output: seen });
                }
            }
        }
    }

    /// The `truewire` binary `start` runs for `project`.
    pub fn binary(project: impl AsRef<Path>) -> PathBuf {
        if let Some(binary) = std::env::var_os("TRUEWIRE_BIN") {
            return PathBuf::from(binary);
        }
        let mut dir = Some(project.as_ref());
        while let Some(here) = dir {
            let candidate = here.join(".venv").join("bin").join("truewire");
            if candidate.is_file() {
                return candidate;
            }
            dir = here.parent();
        }
        PathBuf::from("truewire")
    }
}

impl Drop for Mock {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

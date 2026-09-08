//! Shared harness: `truewire mock` for this project on free ports, and a real `GitHub`
//! client pointed at it, built exactly as a user would.
//!
//! The mock binary is `../../.venv/bin/truewire` (the repository's own environment)
//! unless `TRUEWIRE_BIN` names another one.

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;

use github::core::{Core, CoreOptions};
use github::GitHub;

pub struct Mock {
    child: Child,
    pub http_base_url: String,
}

impl Mock {
    pub fn start() -> Self {
        let root = env!("CARGO_MANIFEST_DIR");
        let bin = std::env::var("TRUEWIRE_BIN")
            .unwrap_or_else(|_| format!("{root}/../../.venv/bin/truewire"));
        let mut child = Command::new(bin)
            .args([
                "mock",
                "--project",
                root,
                "--http-port",
                "0",
                "--ws-port",
                "0",
            ])
            .env("PYTHONUNBUFFERED", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("truewire mock starts");
        let stdout = child.stdout.take().expect("stdout is piped");
        let mut lines = BufReader::new(stdout).lines();
        let http_base_url = loop {
            match lines.next() {
                Some(Ok(line)) => {
                    if let Some(url) = line.strip_prefix("HTTP") {
                        break url.trim().to_string();
                    }
                }
                _ => panic!("truewire mock exited before announcing its HTTP port"),
            }
        };
        // Keep draining its output so the mock never blocks on a full pipe.
        std::thread::spawn(move || for _ in lines {});
        Self {
            child,
            http_base_url,
        }
    }

    /// The generated client against the mock's base URL.
    pub fn client(&self) -> GitHub {
        let core = Core::new(CoreOptions {
            base_url: Some(self.http_base_url.clone()),
            ..CoreOptions::default()
        });
        GitHub::new(Arc::new(core))
    }
}

impl Drop for Mock {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

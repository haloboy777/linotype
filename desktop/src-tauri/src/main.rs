// Linotype desktop shell: starts the Python engine as a sidecar and shows its pages.
//
// The engine prints `LINOTYPE_URL <url>` and `LINOTYPE_TOKEN <token>` on startup. Until both
// arrive the window shows dist/index.html; then it navigates to the engine's drop page, or
// straight to a file if one was opened from Finder / "Open With" / the command line.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::{AppHandle, Manager, RunEvent, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

#[derive(Default)]
struct Engine {
    url: Option<Url>,
    token: Option<String>,
    pending: Vec<PathBuf>,
    child: Option<CommandChild>,
}

fn navigate(app: &AppHandle, url: Url) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.navigate(url);
        let _ = w.set_focus();
    }
}

/// Open a PDF by path once the engine is up; queue it until then.
fn open_path(app: &AppHandle, path: &Path) {
    let state = app.state::<Mutex<Engine>>();
    let mut engine = state.lock().unwrap();
    match (engine.url.clone(), engine.token.clone()) {
        (Some(base), Some(token)) => {
            drop(engine);
            let mut target = base.join("open-path").expect("engine url");
            target
                .query_pairs_mut()
                .append_pair("path", &path.to_string_lossy())
                .append_pair("token", &token);
            navigate(app, target);
        }
        _ => engine.pending.push(path.to_path_buf()),
    }
}

fn on_engine_line(app: &AppHandle, line: &str) {
    let state = app.state::<Mutex<Engine>>();
    if let Some(url) = line.strip_prefix("LINOTYPE_URL ") {
        state.lock().unwrap().url = Url::parse(url.trim()).ok();
    } else if let Some(token) = line.strip_prefix("LINOTYPE_TOKEN ") {
        let (base, pending) = {
            let mut engine = state.lock().unwrap();
            engine.token = Some(token.trim().to_string());
            (engine.url.clone(), std::mem::take(&mut engine.pending))
        };
        match (pending.last(), base) {
            (Some(path), _) => open_path(app, path),
            (None, Some(base)) => navigate(app, base),
            _ => {}
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Mutex::new(Engine::default()))
        .setup(|app| {
            let handle = app.handle().clone();

            // Windows / Linux pass "Open with" files as arguments
            let args: Vec<PathBuf> = std::env::args()
                .skip(1)
                .map(PathBuf::from)
                .filter(|p| p.is_file())
                .collect();

            let (mut rx, child) = app
                .shell()
                .sidecar("linotype-engine")?
                .env("NO_BROWSER", "1")
                .spawn()?;
            {
                let state = app.state::<Mutex<Engine>>();
                let mut engine = state.lock().unwrap();
                engine.child = Some(child);
                engine.pending.extend(args);
            }

            tauri::async_runtime::spawn(async move {
                let mut buf = String::new();
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Stdout(bytes) = event {
                        buf.push_str(&String::from_utf8_lossy(&bytes));
                        if !buf.ends_with('\n') {
                            buf.push('\n');
                        }
                        while let Some(i) = buf.find('\n') {
                            let line: String = buf.drain(..=i).collect();
                            on_engine_line(&handle, line.trim());
                        }
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Linotype")
        .run(|app, event| match event {
            // macOS delivers Finder double-clicks and dock drops as URLs
            #[cfg(target_os = "macos")]
            RunEvent::Opened { urls } => {
                for url in urls {
                    if let Ok(path) = url.to_file_path() {
                        open_path(app, &path);
                    }
                }
            }
            RunEvent::Exit => {
                if let Some(child) = app.state::<Mutex<Engine>>().lock().unwrap().child.take() {
                    let _ = child.kill();
                }
            }
            _ => {}
        });
}

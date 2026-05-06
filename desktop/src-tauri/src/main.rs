use serde::Serialize;
use std::{
    env, fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::Manager;
#[cfg(windows)]
use {std::fs::File, zip::ZipArchive};

const SETUP_VERSION: u64 = 1;
const DEFAULT_WINDOWS_FFMPEG_URL: &str =
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip";

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
    url: Mutex<Option<String>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeProbe {
    app_root: String,
    data_dir: String,
    python_path: Option<String>,
    python_ready: bool,
    ffmpeg_path: Option<String>,
    ffmpeg_ready: bool,
}

#[derive(Serialize)]
struct BackendStarted {
    url: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AssetStatus {
    ffmpeg_ready: bool,
    ffmpeg_path: Option<String>,
    model_ready: bool,
}

fn main() {
    tauri::Builder::default()
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![
            probe_runtime,
            ensure_workspace,
            ensure_external_assets,
            start_backend,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build StemDeck desktop app")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let state = app_handle.state::<BackendState>();
                stop_backend(&state);
            }
        });
}

#[tauri::command]
fn probe_runtime() -> Result<RuntimeProbe, String> {
    let root = app_root()?;
    let data_dir = root.join("data");
    let python = python_path(&root);
    let ffmpeg = ffmpeg_path(&data_dir);
    Ok(RuntimeProbe {
        app_root: root.display().to_string(),
        data_dir: data_dir.display().to_string(),
        python_ready: python.as_ref().is_some_and(|p| p.is_file()),
        python_path: python.map(|p| p.display().to_string()),
        ffmpeg_ready: ffmpeg.as_ref().is_some_and(|p| p.is_file()),
        ffmpeg_path: ffmpeg.map(|p| p.display().to_string()),
    })
}

#[tauri::command]
fn ensure_workspace() -> Result<(), String> {
    let data = app_root()?.join("data");
    for dir in ["cache", "downloads", "ffmpeg", "jobs", "logs", "models"] {
        fs::create_dir_all(data.join(dir))
            .map_err(|e| format!("failed to create data/{dir}: {e}"))?;
    }
    let config = data.join("config.json");
    if !config.exists() {
        fs::write(
            &config,
            "{\n  \"setupVersion\": 1,\n  \"ffmpegReady\": false,\n  \"modelReady\": false\n}\n",
        )
        .map_err(|e| format!("failed to write {}: {e}", config.display()))?;
    }
    Ok(())
}

#[tauri::command]
fn ensure_external_assets() -> Result<AssetStatus, String> {
    ensure_workspace()?;
    let root = app_root()?;
    let data_dir = root.join("data");
    let ffmpeg = ensure_ffmpeg(&data_dir)?;
    write_setup_config(&data_dir, &ffmpeg)?;
    Ok(AssetStatus {
        ffmpeg_ready: true,
        ffmpeg_path: Some(ffmpeg.display().to_string()),
        model_ready: false,
    })
}

#[tauri::command]
fn start_backend(state: tauri::State<BackendState>) -> Result<BackendStarted, String> {
    if let Some(url) = state.url.lock().map_err(|e| e.to_string())?.clone() {
        return Ok(BackendStarted { url });
    }

    let root = app_root()?;
    let backend_dir = backend_dir(&root)?;
    let data_dir = root.join("data");
    let python = python_path(&root).filter(|p| p.is_file()).ok_or_else(|| {
        "Python runtime not found. Expected python/ or .venv/ under StemDeck.".to_string()
    })?;
    let port = free_port()?;
    let url = format!("http://127.0.0.1:{port}");

    let mut cmd = Command::new(python);
    cmd.args([
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        &port.to_string(),
    ]);
    cmd.current_dir(&backend_dir)
        .env("STEMDECK_DATA_DIR", &data_dir)
        .env("STEMDECK_DESKTOP", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("XDG_CACHE_HOME", data_dir.join("cache"))
        .env("TORCH_HOME", data_dir.join("models").join("torch"))
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    if let Some(ffmpeg_dir) = ffmpeg_dir_if_present(&data_dir) {
        let existing = env::var_os("PATH").unwrap_or_default();
        let mut paths = vec![ffmpeg_dir];
        paths.extend(env::split_paths(&existing));
        let joined = env::join_paths(paths).map_err(|e| e.to_string())?;
        cmd.env("PATH", joined);
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to start backend: {e}"))?;
    *state.child.lock().map_err(|e| e.to_string())? = Some(child);

    wait_for_health(port, Duration::from_secs(30))?;
    *state.url.lock().map_err(|e| e.to_string())? = Some(url.clone());
    Ok(BackendStarted { url })
}

fn stop_backend(state: &BackendState) {
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        *guard = None;
    }
}

fn app_root() -> Result<PathBuf, String> {
    if let Ok(root) = env::var("STEMDECK_ROOT") {
        return Ok(PathBuf::from(root));
    }
    if let Ok(cwd) = env::current_dir() {
        if let Some(root) = find_repo_root(&cwd) {
            return Ok(root);
        }
    }
    let exe = env::current_exe().map_err(|e| format!("failed to resolve current exe: {e}"))?;
    let exe_dir = exe
        .parent()
        .ok_or_else(|| "current exe has no parent directory".to_string())?;
    if let Some(root) = find_repo_root(exe_dir) {
        return Ok(root);
    }
    Ok(exe_dir.to_path_buf())
}

fn find_repo_root(start: &Path) -> Option<PathBuf> {
    for candidate in start.ancestors() {
        if candidate.join("pyproject.toml").is_file() && candidate.join("app").is_dir() {
            return Some(candidate.to_path_buf());
        }
        if candidate.join("backend").join("app").is_dir() && candidate.join("python").is_dir() {
            return Some(candidate.to_path_buf());
        }
    }
    None
}

fn backend_dir(root: &Path) -> Result<PathBuf, String> {
    let portable = root.join("backend");
    if portable.join("app").is_dir() {
        return Ok(portable);
    }
    if root.join("app").is_dir() {
        return Ok(root.to_path_buf());
    }
    Err(format!(
        "backend app directory not found under {}",
        root.display()
    ))
}

fn python_path(root: &Path) -> Option<PathBuf> {
    if let Ok(path) = env::var("STEMDECK_PYTHON") {
        return Some(PathBuf::from(path));
    }
    let candidates = if cfg!(windows) {
        vec![
            root.join("python").join("python.exe"),
            root.join(".venv").join("Scripts").join("python.exe"),
        ]
    } else {
        vec![
            root.join("python").join("bin").join("python"),
            root.join(".venv").join("bin").join("python"),
            PathBuf::from("python3"),
        ]
    };
    candidates
        .into_iter()
        .find(|p| p.is_file())
        .or_else(|| Some(PathBuf::from("python3")))
}

fn ffmpeg_path(data_dir: &Path) -> Option<PathBuf> {
    if let Ok(path) = env::var("STEMDECK_FFMPEG") {
        return Some(PathBuf::from(path));
    }
    let file = if cfg!(windows) {
        "ffmpeg.exe"
    } else {
        "ffmpeg"
    };
    Some(data_dir.join("ffmpeg").join(file))
}

fn ffprobe_path(data_dir: &Path) -> PathBuf {
    let file = if cfg!(windows) {
        "ffprobe.exe"
    } else {
        "ffprobe"
    };
    data_dir.join("ffmpeg").join(file)
}

fn ffmpeg_dir_if_present(data_dir: &Path) -> Option<PathBuf> {
    let path = ffmpeg_path(data_dir)?;
    if path.is_file() {
        path.parent().map(Path::to_path_buf)
    } else {
        None
    }
}

fn free_port() -> Result<u16, String> {
    let listener =
        TcpListener::bind("127.0.0.1:0").map_err(|e| format!("port bind failed: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    drop(listener);
    Ok(port)
}

fn wait_for_health(port: u16, timeout: Duration) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    loop {
        if Instant::now() >= deadline {
            return Err("backend did not become healthy before timeout".to_string());
        }
        if health_once(port).is_ok() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
}

fn health_once(port: u16) -> Result<(), String> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| e.to_string())?;
    stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .map_err(|e| e.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|e| e.to_string())?;
    if response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200") {
        Ok(())
    } else {
        Err("health endpoint did not return 200".to_string())
    }
}

fn ensure_ffmpeg(data_dir: &Path) -> Result<PathBuf, String> {
    let portable =
        ffmpeg_path(data_dir).ok_or_else(|| "failed to resolve FFmpeg path".to_string())?;
    if portable.is_file() {
        verify_ffmpeg(&portable)?;
        return Ok(portable);
    }

    #[cfg(windows)]
    {
        download_windows_ffmpeg(data_dir)?;
        let portable =
            ffmpeg_path(data_dir).ok_or_else(|| "failed to resolve FFmpeg path".to_string())?;
        verify_ffmpeg(&portable)?;
        return Ok(portable);
    }

    #[cfg(not(windows))]
    {
        verify_ffmpeg(Path::new("ffmpeg"))?;
        Ok(PathBuf::from("ffmpeg"))
    }
}

#[cfg(windows)]
fn download_windows_ffmpeg(data_dir: &Path) -> Result<(), String> {
    let url = env::var("STEMDECK_FFMPEG_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_WINDOWS_FFMPEG_URL.to_string());
    let downloads = data_dir.join("downloads");
    let archive_path = downloads.join("ffmpeg-windows.zip");
    fs::create_dir_all(&downloads)
        .map_err(|e| format!("failed to create {}: {e}", downloads.display()))?;

    download_file_with_powershell(&url, &archive_path)?;

    extract_ffmpeg_binaries(&archive_path, data_dir)
}

#[cfg(windows)]
fn download_file_with_powershell(url: &str, target: &Path) -> Result<(), String> {
    let script = concat!(
        "$ProgressPreference = 'SilentlyContinue'; ",
        "Invoke-WebRequest -Uri $args[0] -OutFile $args[1]"
    );
    let target_arg = target.display().to_string();
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
            url,
            &target_arg,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    hide_console_window(&mut command);
    let output = command
        .output()
        .map_err(|e| format!("failed to start FFmpeg download: {e}"))?;
    if output.status.success() && target.is_file() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!(
            "failed to download FFmpeg from {url}: {}",
            stderr.trim()
        ))
    }
}

#[cfg(windows)]
fn extract_ffmpeg_binaries(archive_path: &Path, data_dir: &Path) -> Result<(), String> {
    let file = File::open(archive_path)
        .map_err(|e| format!("failed to open {}: {e}", archive_path.display()))?;
    let mut archive = ZipArchive::new(file)
        .map_err(|e| format!("failed to read FFmpeg zip {}: {e}", archive_path.display()))?;
    let ffmpeg_dir = data_dir.join("ffmpeg");
    fs::create_dir_all(&ffmpeg_dir)
        .map_err(|e| format!("failed to create {}: {e}", ffmpeg_dir.display()))?;

    let mut copied_ffmpeg = false;
    let mut copied_ffprobe = false;
    for i in 0..archive.len() {
        let mut entry = archive
            .by_index(i)
            .map_err(|e| format!("failed to read FFmpeg zip entry {i}: {e}"))?;
        if !entry.is_file() {
            continue;
        }
        let Some(name) = entry.enclosed_name().map(Path::to_path_buf) else {
            continue;
        };
        let Some(file_name) = name.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        let target_name = match file_name.to_ascii_lowercase().as_str() {
            "ffmpeg.exe" => {
                copied_ffmpeg = true;
                "ffmpeg.exe"
            }
            "ffprobe.exe" => {
                copied_ffprobe = true;
                "ffprobe.exe"
            }
            _ => continue,
        };
        let target = ffmpeg_dir.join(target_name);
        let mut output = File::create(&target)
            .map_err(|e| format!("failed to create {}: {e}", target.display()))?;
        std::io::copy(&mut entry, &mut output)
            .map_err(|e| format!("failed to extract {}: {e}", target.display()))?;
    }

    if !copied_ffmpeg {
        return Err("downloaded FFmpeg archive did not contain ffmpeg.exe".to_string());
    }
    if !copied_ffprobe {
        return Err("downloaded FFmpeg archive did not contain ffprobe.exe".to_string());
    }
    Ok(())
}

fn verify_ffmpeg(path: &Path) -> Result<(), String> {
    let mut command = Command::new(path);
    command
        .arg("-version")
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    hide_console_window(&mut command);
    let output = command
        .output()
        .map_err(|e| format!("failed to run FFmpeg at {}: {e}", path.display()))?;
    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!(
            "FFmpeg at {} failed verification: {}",
            path.display(),
            stderr.trim()
        ))
    }
}

fn write_setup_config(data_dir: &Path, ffmpeg: &Path) -> Result<(), String> {
    let config_path = data_dir.join("config.json");
    let ffprobe = ffprobe_path(data_dir);
    let updated_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    let config = serde_json::json!({
        "setupVersion": SETUP_VERSION,
        "ffmpegReady": true,
        "ffmpegPath": ffmpeg.display().to_string(),
        "ffprobeReady": ffprobe.is_file(),
        "ffprobePath": ffprobe.display().to_string(),
        "ffmpegSource": env::var("STEMDECK_FFMPEG_URL").unwrap_or_else(|_| DEFAULT_WINDOWS_FFMPEG_URL.to_string()),
        "modelReady": false,
        "updatedAt": updated_at
    });
    let body = serde_json::to_string_pretty(&config)
        .map_err(|e| format!("failed to serialize setup config: {e}"))?;
    fs::write(&config_path, body + "\n")
        .map_err(|e| format!("failed to write {}: {e}", config_path.display()))
}

#[cfg(windows)]
fn hide_console_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console_window(_command: &mut Command) {}

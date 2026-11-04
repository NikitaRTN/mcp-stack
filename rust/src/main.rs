use mcp_hub_rs::Client;
use serde_json::Value;

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 2 && args[1] == "serve" {
        return serve(None);
    }
    if args.len() == 4 && args[1] == "serve" && args[2] == "--config" {
        return serve(Some(&args[3]));
    }
    if args.len() == 2 && matches!(args[1].as_str(), "--help" | "-h") {
        println!("MCP Hub (experimental)\n\n  mcp-hub-rs serve --config PATH\n  mcp-hub-rs list URL\n  mcp-hub-rs call URL TOOL JSON_OBJECT\n\nSet MCP_HUB_TOKEN in the environment; optional MCP_HUB_PORT defaults to 8765.\nThe server listens on loopback. See rust/README.md for configuration and limitations.");
        return Ok(());
    }
    if args.len() == 2 && args[1] == "--version" {
        println!(
            "mcp-hub-rs {} (experimental MCP hub)",
            env!("CARGO_PKG_VERSION")
        );
        return Ok(());
    }
    if args.len() < 3 {
        return Err("Usage: mcp-hub-rs serve --config PATH | list URL | call URL TOOL JSON_OBJECT\nBearer token: MCP_HUB_TOKEN environment variable".into());
    }
    let token = std::env::var("MCP_HUB_TOKEN").ok();
    let mut client = Client::new(&args[2], token)?;
    let result = match (args[1].as_str(), args.len()) {
        ("list", 3) => client.list_tools().map(Value::Array),
        ("call", 5) => {
            let arguments: Value =
                serde_json::from_str(&args[4]).map_err(|_| "Invalid JSON arguments")?;
            client.call_tool(&args[3], arguments)
        }
        _ => Err("Usage: mcp-hub-rs list URL | call URL TOOL JSON_OBJECT".into()),
    };
    client.close();
    println!(
        "{}",
        serde_json::to_string_pretty(&result?).map_err(|_| "Cannot encode response")?
    );
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

fn serve(simple_config: Option<&str>) -> Result<(), String> {
    use mcp_hub_rs::server::{Proxy, Settings};
    use std::sync::{atomic::AtomicBool, Arc};
    let port = std::env::var("MCP_HUB_PORT")
        .unwrap_or_else(|_| "8765".into())
        .parse::<u16>()
        .map_err(|_| "invalid MCP_HUB_PORT")?;
    let db = std::env::var_os("MCP_HUB_DB").map(Into::into);
    let (mut proxy, suffix) = if let Some(path) = simple_config {
        if [
            "MCP_HUB_STDIO_CONFIG",
            "MCP_HUB_CONFIG",
            "MCP_HUB_UPSTREAM",
            "MCP_HUB_UPSTREAM_TOKEN",
            "MCP_HUB_PROCESS_CONFIG",
        ]
        .iter()
        .any(|name| std::env::var_os(name).is_some())
        {
            return Err("--config cannot be combined with legacy route configuration".into());
        }
        let config_path = std::fs::canonicalize(path).map_err(|_| "cannot open hub config")?;
        let root = config_path.parent().ok_or("invalid hub config path")?;
        let file = std::fs::File::open(&config_path).map_err(|_| "cannot open hub config")?;
        let (routes, processes) = mcp_hub_rs::simple::read_config(file, root)?;
        let token = std::env::var("MCP_HUB_TOKEN").map_err(|_| "MCP_HUB_TOKEN is required")?;
        if !processes.is_empty() {
            std::fs::create_dir_all(root.join("logs"))
                .map_err(|_| "cannot create log directory")?;
        }
        (
            Proxy::bind_mixed(port, token, routes, processes, db)?,
            "/mcp[/service-id]",
        )
    } else if let Some(path) = std::env::var_os("MCP_HUB_STDIO_CONFIG") {
        if [
            "MCP_HUB_CONFIG",
            "MCP_HUB_UPSTREAM",
            "MCP_HUB_UPSTREAM_TOKEN",
            "MCP_HUB_PROCESS_CONFIG",
        ]
        .iter()
        .any(|name| std::env::var_os(name).is_some())
        {
            return Err(
                "stdio config cannot be combined with HTTP or managed-process configuration".into(),
            );
        }
        let file = std::fs::File::open(path).map_err(|_| "cannot open stdio config")?;
        let specs = mcp_hub_rs::managed::read_config(file)?;
        let token = std::env::var("MCP_HUB_TOKEN").map_err(|_| "MCP_HUB_TOKEN is required")?;
        (
            Proxy::bind_stdio(port, token, specs, db)?,
            "/mcp[/service-id]",
        )
    } else if let Some(path) = std::env::var_os("MCP_HUB_CONFIG") {
        if std::env::var_os("MCP_HUB_UPSTREAM").is_some()
            || std::env::var_os("MCP_HUB_UPSTREAM_TOKEN").is_some()
        {
            return Err("MCP_HUB_CONFIG cannot be combined with upstream overrides".into());
        }
        let file = std::fs::File::open(path).map_err(|_| "cannot open legacy config")?;
        let (legacy_token, routes) = mcp_hub_rs::server::legacy_routes(file)?;
        let token = std::env::var("MCP_HUB_TOKEN").unwrap_or(legacy_token);
        (Proxy::bind_routes(port, token, routes, db)?, "")
    } else {
        let settings = Settings {
            port,
            upstream: std::env::var("MCP_HUB_UPSTREAM")
                .map_err(|_| "MCP_HUB_UPSTREAM is required")?,
            access_token: std::env::var("MCP_HUB_TOKEN")
                .map_err(|_| "MCP_HUB_TOKEN is required")?,
            upstream_token: std::env::var("MCP_HUB_UPSTREAM_TOKEN").ok(),
            telemetry_db: db,
        };
        (Proxy::bind(settings)?, "/mcp")
    };
    let oauth_names = [
        "MCP_HUB_OAUTH_TOKEN_URL",
        "MCP_HUB_OAUTH_CLIENT_ID",
        "MCP_HUB_OAUTH_CLIENT_SECRET",
        "MCP_HUB_OAUTH_SCOPE",
        "MCP_HUB_OAUTH_AUDIENCE",
        "MCP_HUB_OAUTH_AUTH_METHOD",
        "MCP_HUB_OAUTH_ROUTE",
    ];
    if oauth_names
        .iter()
        .any(|name| std::env::var_os(name).is_some())
    {
        let required =
            |name| std::env::var(name).map_err(|_| "incomplete OAuth client configuration");
        let settings = mcp_hub_rs::oauth_client::Settings {
            token_url: required("MCP_HUB_OAUTH_TOKEN_URL")?,
            client_id: required("MCP_HUB_OAUTH_CLIENT_ID")?,
            client_secret: required("MCP_HUB_OAUTH_CLIENT_SECRET")?,
            scope: std::env::var("MCP_HUB_OAUTH_SCOPE").ok(),
            audience: std::env::var("MCP_HUB_OAUTH_AUDIENCE").ok(),
            auth_method: std::env::var("MCP_HUB_OAUTH_AUTH_METHOD")
                .unwrap_or_else(|_| "basic".into()),
        };
        let route = std::env::var("MCP_HUB_OAUTH_ROUTE").unwrap_or_else(|_| "default".into());
        proxy.enable_upstream_oauth(&route, settings)?;
    }
    let introspection_names = [
        "MCP_HUB_INTROSPECTION_URL",
        "MCP_HUB_INTROSPECTION_CLIENT_ID",
        "MCP_HUB_INTROSPECTION_CLIENT_SECRET",
        "MCP_HUB_INTROSPECTION_AUTH_METHOD",
        "MCP_HUB_INTROSPECTION_REQUIRED_SCOPES",
        "MCP_HUB_INTROSPECTION_AUDIENCE",
        "MCP_HUB_INTROSPECTION_ISSUER",
        "MCP_HUB_INTROSPECTION_ROUTE",
    ];
    if introspection_names
        .iter()
        .any(|name| std::env::var_os(name).is_some())
    {
        let required = |name| {
            std::env::var(name).map_err(|_| "incomplete inbound introspection configuration")
        };
        let settings = mcp_hub_rs::oauth_introspection::Settings {
            endpoint: required("MCP_HUB_INTROSPECTION_URL")?,
            client_id: required("MCP_HUB_INTROSPECTION_CLIENT_ID")?,
            client_secret: required("MCP_HUB_INTROSPECTION_CLIENT_SECRET")?,
            auth_method: std::env::var("MCP_HUB_INTROSPECTION_AUTH_METHOD")
                .unwrap_or_else(|_| "basic".into()),
            required_scopes: std::env::var("MCP_HUB_INTROSPECTION_REQUIRED_SCOPES")
                .unwrap_or_default()
                .split_ascii_whitespace()
                .map(str::to_owned)
                .collect(),
            audience: std::env::var("MCP_HUB_INTROSPECTION_AUDIENCE").ok(),
            issuer: std::env::var("MCP_HUB_INTROSPECTION_ISSUER").ok(),
        };
        let route =
            std::env::var("MCP_HUB_INTROSPECTION_ROUTE").unwrap_or_else(|_| "default".into());
        proxy.enable_inbound_oauth(&route, settings)?;
    }
    let builtin_file = std::env::var_os("MCP_HUB_BUILTIN_CONFIG");
    let builtin_key = std::env::var("MCP_HUB_BUILTIN_SIGNING_KEY").ok();
    match (builtin_file, builtin_key) {
        (Some(path), Some(key)) => {
            use std::io::Read;
            if key.len() < 32 || key.len() > 4096 {
                return Err("invalid built-in OAuth signing key".into());
            }
            let file =
                std::fs::File::open(path).map_err(|_| "cannot open built-in OAuth config")?;
            let mut bytes = Vec::new();
            file.take(100_001)
                .read_to_end(&mut bytes)
                .map_err(|_| "cannot read built-in OAuth config")?;
            if bytes.len() > 100_000 {
                return Err("built-in OAuth config too large".into());
            }
            let v: Value =
                serde_json::from_slice(&bytes).map_err(|_| "invalid built-in OAuth config")?;
            let text = |name| {
                v.get(name)
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .ok_or("incomplete built-in OAuth config")
            };
            let scopes: Vec<String> = serde_json::from_value(
                v.get("scopes")
                    .cloned()
                    .ok_or("incomplete built-in OAuth config")?,
            )
            .map_err(|_| "invalid OAuth scopes")?;
            let config = mcp_hub_rs::oauth_builtin_http::Config {
                database: text("database")?.into(),
                signing_key: key,
                auth: v
                    .get("auth")
                    .cloned()
                    .ok_or("incomplete built-in OAuth config")?,
                base: text("base")?,
                path: String::new(),
                scopes,
                proxy_key: std::env::var("MCP_HUB_TRUSTED_PROXY_KEY").ok(),
            };
            let route = v.get("route").and_then(Value::as_str).unwrap_or("default");
            proxy.enable_builtin_oauth(route, config)?;
        }
        (None, None) if std::env::var_os("MCP_HUB_TRUSTED_PROXY_KEY").is_none() => {}
        _ => {
            return Err(
                "MCP_HUB_BUILTIN_CONFIG and MCP_HUB_BUILTIN_SIGNING_KEY must be set together"
                    .into(),
            )
        }
    }
    match (
        std::env::var_os("MCP_HUB_PROCESS_CONFIG"),
        std::env::var("MCP_HUB_ADMIN_TOKEN").ok(),
    ) {
        (Some(path), Some(admin_token)) => {
            let file = std::fs::File::open(path).map_err(|_| "cannot open process config")?;
            proxy.enable_management(admin_token, mcp_hub_rs::managed::read_config(file)?)?;
        }
        (None, None) => {}
        _ => {
            return Err(
                "MCP_HUB_PROCESS_CONFIG and MCP_HUB_ADMIN_TOKEN must be set together".into(),
            )
        }
    }
    let proxy = Arc::new(proxy);
    eprintln!(
        "Experimental MCP proxy listening at http://{}{suffix}",
        proxy.address()
    );
    let shutdown = Arc::new(AtomicBool::new(false));
    let signal = shutdown.clone();
    ctrlc::set_handler(move || signal.store(true, std::sync::atomic::Ordering::Relaxed))
        .map_err(|_| "cannot install shutdown handler")?;
    proxy.run(shutdown)
}

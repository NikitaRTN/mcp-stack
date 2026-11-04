use mcp_hub_rs::Client;
use serde_json::Value;

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 2 && args[1] == "serve" {
        return serve();
    }
    if args.len() == 2 && args[1] == "--version" {
        println!(
            "mcp-hub-rs {} (experimental MCP client)",
            env!("CARGO_PKG_VERSION")
        );
        return Ok(());
    }
    if args.len() < 3 {
        return Err("Usage: mcp-hub-rs list URL | call URL TOOL JSON_OBJECT\nBearer token: MCP_HUB_TOKEN environment variable".into());
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

fn serve() -> Result<(), String> {
    use mcp_hub_rs::server::{Proxy, Settings};
    use std::sync::{atomic::AtomicBool, Arc};
    let port = std::env::var("MCP_HUB_PORT")
        .unwrap_or_else(|_| "8765".into())
        .parse::<u16>()
        .map_err(|_| "invalid MCP_HUB_PORT")?;
    let db = std::env::var_os("MCP_HUB_DB").map(Into::into);
    let (proxy, suffix) = if let Some(path) = std::env::var_os("MCP_HUB_CONFIG") {
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
    let proxy = Arc::new(proxy);
    eprintln!(
        "Experimental MCP proxy listening at http://{}{suffix}",
        proxy.address()
    );
    proxy.run(Arc::new(AtomicBool::new(false)))
}

use mcp_hub_rs::Client;
use serde_json::Value;

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
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

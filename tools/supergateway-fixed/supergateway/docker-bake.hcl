variable "VERSION" {
  default = "DEV"
}

target "common" {
  context   = "."
  platforms = ["linux/amd64", "linux/arm64"]
}

group "default" {
  targets = ["base", "uvx", "deno"]
}

target "base" {
  inherits   = ["common"]
  dockerfile = "docker/base.Dockerfile"
  tags = [
    "supercorp/supergateway:latest",
    "supercorp/supergateway:base",
    "supercorp/supergateway:${VERSION}",
    "ghcr.io/supercorp-ai/supergateway:latest",
    "ghcr.io/supercorp-ai/supergateway:base",
    "ghcr.io/supercorp-ai/supergateway:${VERSION}"
  ]
}

target "uvx" {
  inherits   = ["common"]
  depends_on  = ["base"]
  dockerfile = "docker/uvx.Dockerfile"
  contexts = { base = "target:base" }
  tags = [
    "supercorp/supergateway:uvx",
    "supercorp/supergateway:${VERSION}-uvx",
    "ghcr.io/supercorp-ai/supergateway:uvx",
    "ghcr.io/supercorp-ai/supergateway:${VERSION}-uvx"
  ]
}

target "deno" {
  inherits   = ["common"]
  depends_on  = ["base"]
  dockerfile = "docker/deno.Dockerfile"
  contexts = { base = "target:base" }
  tags = [
    "supercorp/supergateway:deno",
    "supercorp/supergateway:${VERSION}-deno",
    "ghcr.io/supercorp-ai/supergateway:deno",
    "ghcr.io/supercorp-ai/supergateway:${VERSION}-deno"
  ]
}

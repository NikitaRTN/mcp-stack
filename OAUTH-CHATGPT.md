# OAuth для ChatGPT

## Быстрое подключение

1. Задайте пароль администратора MCP Hub в локальной панели.
2. Убедитесь, что домен доступен через публичный HTTPS на порту 443.
3. В **MCP Studio → Конфигурация → Доступ к публичному маршруту** выберите **OAuth для ChatGPT**.
4. В блоке **OAuth-защита маршрута** оставьте **Автоматически — встроенный OAuth**.
5. Сохраните изменения, включите MCP и перезапустите Caddy/Hub.
6. В ChatGPT укажите только адрес MCP, например `https://mcp.riseshield.ru/roblox`.

ChatGPT автоматически обнаружит OAuth metadata, зарегистрирует PKCE-клиент и откроет страницу MCP Hub с вводом пароля.

## Если ChatGPT просит заполнить endpoints вручную

Для маршрута `/roblox`:

- Auth URL: `https://mcp.riseshield.ru/oauth/authorize`
- Token URL: `https://mcp.riseshield.ru/oauth/token`
- Registration URL: `https://mcp.riseshield.ru/oauth/register`
- Authorization server base: `https://mcp.riseshield.ru`
- Resource: `https://mcp.riseshield.ru/roblox`

Для другого MCP измените только Resource на его публичный адрес.

## Проверка discovery

Эти URL должны возвращать JSON:

- `https://mcp.riseshield.ru/.well-known/oauth-authorization-server`
- `https://mcp.riseshield.ru/.well-known/oauth-protected-resource/roblox`

## Безопасность

- Поддерживается Authorization Code с обязательным PKCE S256.
- Динамические клиенты работают без `client_secret` и привязаны к точному redirect URI.
- Access token привязан к конкретному MCP resource.
- Пароль проверяется локально и не передаётся ChatGPT.
- Режим **Внешний OAuth — расширенный** нужен только для Keycloak, Auth0, Authentik или другого RFC 7662 introspection endpoint.

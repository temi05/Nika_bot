import re

path = 'app/services/ai_service.py'
content = open(path, 'r', encoding='utf-8').read()

# Ищем блок _tool_moderate_user до строки target = self.db.search_user
old_pattern = re.compile(
    r'(    async def _tool_moderate_user\(self, chat_id: int, caller_is_admin: bool, args: dict\[str, Any\]\) -> str:\s*)'
    r'(        action = str\(args\.get\("action"\) or ""\)\.lower\(\)\s*)'
    r'(        target_name = str\(args\.get\("target_name"\) or ""\)\.strip\(\)\s*)'
    r'(        reason = str\(args\.get\("reason"\) or ""\)\.strip\(\)\s*)'
    r'(        value = int\(args\.get\("value"\) or 0\)\s*)'
    r'(\s*        if not target_name:\s*return "Не указано, к кому применять действие\."\s*)'
    r'(\s*        target = self\.db\.search_user\(chat_id, target_name\)\s*)'
    r'        if not target:\s*\n\s*return f"Не вижу пользователя \{target_name\}\."',
    re.DOTALL
)

new_block = '''    async def _tool_moderate_user(self, chat_id: int, caller_is_admin: bool, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        explicit_uid = args.get("target_user_id")
        reason = str(args.get("reason") or "").strip()
        value = int(args.get("value") or 0)

        if not target_name and not explicit_uid:
            return "Не указано, к кому применять действие."

        target = None
        if explicit_uid:
            try:
                target = self.db.get_user_by_platform_id(chat_id, int(explicit_uid))
            except (TypeError, ValueError):
                pass
        if not target:
            target = self.db.search_user(chat_id, target_name)
        if not target:
            return f"Не вижу пользователя '{target_name}'.'''

# Простой поиск по ключевым строкам
marker_start = '    async def _tool_moderate_user(self, chat_id: int, caller_is_admin: bool, args: dict[str, Any]) -> str:'
marker_end = 'if not target:\n            return f"Не вижу пользователя {target_name}."'

if marker_start in content and 'Не вижу пользователя {target_name}' in content:
    # Найти позицию
    start_idx = content.index(marker_start)
    end_marker = 'return f"Не вижу пользователя {target_name}."'
    end_idx = content.index(end_marker, start_idx) + len(end_marker)
    
    old_block = content[start_idx:end_idx]
    print("Found block:")
    print(repr(old_block[:300]))
    
    replacement = '''    async def _tool_moderate_user(self, chat_id: int, caller_is_admin: bool, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        explicit_uid = args.get("target_user_id")
        reason = str(args.get("reason") or "").strip()
        value = int(args.get("value") or 0)

        if not target_name and not explicit_uid:
            return "Не указано, к кому применять действие."

        target = None
        if explicit_uid:
            try:
                target = self.db.get_user_by_platform_id(chat_id, int(explicit_uid))
            except (TypeError, ValueError):
                pass
        if not target:
            target = self.db.search_user(chat_id, target_name)
        if not target:
            return f"Не вижу пользователя '{target_name}'."'''
    
    new_content = content[:start_idx] + replacement + content[end_idx:]
    open(path, 'w', encoding='utf-8').write(new_content)
    print("OK: patched moderate_user")
else:
    print("NOT FOUND marker")
    print("Has start?", marker_start in content)
    print("Has end?", 'Не вижу пользователя {target_name}' in content)

# Теперь патчим _resolve_target_user
content = open(path, 'r', encoding='utf-8').read()
resolve_marker = '    def _resolve_target_user(self, chat_id: int, sender: Sender, target_name: str) -> ChatUser | None:'
if resolve_marker in content:
    start_idx = content.index(resolve_marker)
    end_marker = 'return self.db.search_user(chat_id, target_name)'
    end_idx = content.index(end_marker, start_idx) + len(end_marker)
    
    replacement = '''    def _resolve_target_user(self, chat_id: int, sender: Sender, target_name: str) -> ChatUser | None:
        normalized = (target_name or "").strip().lower()
        if not normalized:
            return None

        # Поиск по числовому user_id
        if re.fullmatch(r"-?\\d+", normalized):
            found = self.db.get_user_by_platform_id(chat_id, int(normalized))
            if found:
                return found

        # Псевдонимы "себя"
        aliases = {"я", "me", "мой", "мне", "себе", "себя",
                   sender.display_name.lower(), sender.first_name.lower()}
        if sender.username:
            aliases.add(sender.username.lower())
            aliases.add(f"@{sender.username.lower()}")
        if normalized in aliases:
            return self.db.get_or_create_user(chat_id, sender)

        # Расширенный поиск с fuzzy + транслитерацией
        return self.db.search_user(chat_id, target_name)'''
    
    new_content = content[:start_idx] + replacement + content[end_idx:]
    open(path, 'w', encoding='utf-8').write(new_content)
    print("OK: patched _resolve_target_user")
else:
    print("resolve_target_user NOT FOUND")

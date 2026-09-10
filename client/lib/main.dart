import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MistralMessengerApp());
}

class MistralMessengerApp extends StatelessWidget {
  const MistralMessengerApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Mistral Messenger Assistant',
      theme: ThemeData(colorSchemeSeed: Colors.deepPurple, useMaterial3: true),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final backend = TextEditingController(text: 'https://bridge.example.com');
  final token = TextEditingController();
  final message = TextEditingController();
  static const secure = FlutterSecureStorage();
  String log = 'Noch nicht verbunden.';
  bool busy = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final p = await SharedPreferences.getInstance();
    backend.text = p.getString('backend') ?? backend.text;
    token.text = await secure.read(key: 'adminToken') ?? '';
  }

  Future<void> _save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('backend', backend.text.trim().replaceAll(RegExp(r'/+$'), ''));
    await secure.write(key: 'adminToken', value: token.text.trim());
  }

  Map<String, String> get _headers => {
        'Authorization': 'Bearer ${token.text.trim()}',
        'Content-Type': 'application/json',
      };

  Future<void> _run(Future<void> Function() fn) async {
    setState(() => busy = true);
    try {
      await _save();
      await fn();
    } catch (e) {
      setState(() => log = 'Fehler: $e');
    } finally {
      setState(() => busy = false);
    }
  }

  Future<void> _status() => _run(() async {
        final r = await http.get(Uri.parse('${backend.text}/admin/status'), headers: _headers);
        if (r.statusCode >= 400) throw Exception('${r.statusCode}: ${r.body}');
        final d = jsonDecode(r.body) as Map<String, dynamic>;
        setState(() => log = const JsonEncoder.withIndent('  ').convert(d));
      });

  Future<void> _pair() => _run(() async {
        final r = await http.post(Uri.parse('${backend.text}/admin/pair'), headers: _headers);
        if (r.statusCode >= 400) throw Exception('${r.statusCode}: ${r.body}');
        final d = jsonDecode(r.body) as Map<String, dynamic>;
        final url = d['url'] as String?;
        setState(() => log = const JsonEncoder.withIndent('  ').convert(d));
        if (url != null && url.isNotEmpty) {
          await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
        }
      });

  Future<void> _chat() => _run(() async {
        final text = message.text.trim();
        if (text.isEmpty) return;
        final r = await http.post(
          Uri.parse('${backend.text}/admin/chat'),
          headers: _headers,
          body: jsonEncode({'message': text, 'session': 'app-test'}),
        );
        if (r.statusCode >= 400) throw Exception('${r.statusCode}: ${r.body}');
        final d = jsonDecode(r.body) as Map<String, dynamic>;
        setState(() => log = d['text']?.toString() ?? r.body);
      });

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Mistral Messenger Assistant')),
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 760),
          child: ListView(
            padding: const EdgeInsets.all(20),
            children: [
              const Text('Backend', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
              const SizedBox(height: 12),
              TextField(controller: backend, decoration: const InputDecoration(labelText: 'Backend URL', border: OutlineInputBorder())),
              const SizedBox(height: 12),
              TextField(controller: token, obscureText: true, decoration: const InputDecoration(labelText: 'Admin Token', border: OutlineInputBorder())),
              const SizedBox(height: 12),
              Wrap(spacing: 10, runSpacing: 10, children: [
                FilledButton(onPressed: busy ? null : _status, child: const Text('Status prüfen')),
                FilledButton.tonal(onPressed: busy ? null : _pair, child: const Text('Telegram verbinden')),
              ]),
              const Divider(height: 36),
              const Text('KI testen', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
              const SizedBox(height: 12),
              TextField(controller: message, minLines: 2, maxLines: 5, decoration: const InputDecoration(labelText: 'Nachricht', border: OutlineInputBorder())),
              const SizedBox(height: 12),
              FilledButton(onPressed: busy ? null : _chat, child: const Text('An KI senden')),
              const SizedBox(height: 20),
              if (busy) const LinearProgressIndicator(),
              const SizedBox(height: 12),
              SelectableText(log),
            ],
          ),
        ),
      ),
    );
  }
}

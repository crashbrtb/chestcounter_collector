"""
Declarative description of every application parameter.

The GUI, the default configuration file and the validation of config.json are
all generated from this single list, so a new parameter only has to be declared
here once to become visible, editable and persisted.

Field types
    str / password / path : free text (password is masked in the GUI)
    int / float           : numeric, optionally bounded by minimum/maximum
    bool                  : checkbox
    choice                : dropdown restricted to `choices`
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass(frozen=True)
class Field:
    key: str
    type: str
    label: str
    default: Any
    help: str = ""
    choices: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    @property
    def is_secret(self) -> bool:
        return self.type == "password"


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    description: str
    fields: List[Field] = field(default_factory=list)


SECTIONS: List[Section] = [
    Section(
        "execution",
        "Execução",
        "Como o coletor se comporta quando o agendador do Windows dispara o run.bat.",
        [
            Field("module", "choice", "Módulo executado", "chests",
                  "Rotina disparada pelo run.bat. 'chests' coleta os baús do clã.",
                  choices=("chests", "journal", "chat", "all")),
            Field("close_browser_on_finish", "bool", "Fechar o navegador ao terminar", True,
                  "Desligue para inspecionar a tela do jogo depois da coleta."),
            Field("stop_on_error", "bool", "Parar no primeiro erro", False,
                  "Ligado, um perfil que falha interrompe a execução inteira. Desligado, o coletor "
                  "registra o erro e segue para o próximo perfil."),
            Field("retries_per_profile", "int", "Tentativas por perfil", 2,
                  "Quantas vezes um perfil é reprocessado antes de ser considerado perdido.",
                  minimum=1, maximum=5),
            Field("log_level", "choice", "Nível do log", "INFO",
                  "DEBUG registra cada leitura de OCR e cada clique.",
                  choices=("DEBUG", "INFO", "WARNING", "ERROR")),
            Field("log_dir", "path", "Pasta dos logs", "execution_logs",
                  "Relativa à pasta do projeto, ou um caminho absoluto."),
            Field("log_retention_days", "int", "Dias de log mantidos", 7,
                  "Logs mais antigos que isso são apagados no início de cada execução. 0 = nunca apagar.",
                  minimum=0, maximum=3650),
            Field("screenshot_on_error", "bool", "Salvar print quando der erro", True,
                  "Grava a tela do jogo em execution_logs/screenshots para diagnóstico."),
        ],
    ),
    Section(
        "browser",
        "Navegador",
        "Chrome controlado por CDP (Chrome DevTools Protocol). Substitui o aplicativo desktop.",
        [
            Field("cdp_host", "str", "Host do CDP", "127.0.0.1",
                  "Praticamente sempre 127.0.0.1."),
            Field("cdp_port", "int", "Porta do CDP", 9222,
                  "Porta de depuração remota. Mude se já houver outro Chrome usando a 9222.",
                  minimum=1024, maximum=65535),
            Field("game_url", "str", "URL do jogo", "https://totalbattle.com/en/",
                  "Endereço aberto no início de cada conta."),
            Field("game_url_filter", "str", "Filtro da aba do jogo", "totalbattle.com",
                  "Trecho da URL usado para reconhecer a aba do jogo entre as abas abertas."),
            Field("executable_path", "path", "Caminho do Chrome", "",
                  "Vazio = procurar Chrome, Edge e Brave nos caminhos padrão do Windows."),
            Field("user_data_dir", "path", "Perfil do navegador", "",
                  "Pasta de dados do Chrome usada pelo coletor. Vazio = "
                  "%USERPROFILE%/.total_battle_chest_profile. Um perfil próprio evita "
                  "interferir no Chrome do dia a dia."),
            Field("reuse_existing", "bool", "Reaproveitar navegador já aberto", True,
                  "Se a porta CDP já estiver respondendo, conecta nele em vez de abrir outro."),
            Field("start_maximized", "bool", "Abrir maximizado", True, ""),
            Field("window_width", "int", "Largura da janela", 1920,
                  "Usada apenas quando 'Abrir maximizado' está desligado.", minimum=800, maximum=7680),
            Field("window_height", "int", "Altura da janela", 1080,
                  "Usada apenas quando 'Abrir maximizado' está desligado.", minimum=600, maximum=4320),
            Field("page_load_timeout", "float", "Tempo limite de carregamento (s)", 90.0,
                  "Espera máxima pelo carregamento do jogo depois de abrir a URL.",
                  minimum=10.0, maximum=600.0),
        ],
    ),
    Section(
        "login",
        "Login",
        "Autenticação no formulário do site. Os seletores vazios são descobertos automaticamente.",
        [
            Field("enabled", "bool", "Fazer login automático", True,
                  "Desligue se preferir deixar a sessão já logada no perfil do navegador."),
            Field("clear_session_between_accounts", "bool", "Encerrar sessão ao trocar de conta", False,
                  "MANTENHA DESLIGADO. Limpar cookies faz o jogo tratar o navegador como um "
                  "aparelho novo e enviar um código de verificação por e-mail — que uma execução "
                  "agendada não tem como responder. Para várias contas, dê a cada uma o seu "
                  "'Perfil do navegador' em Contas e perfis, em vez de ligar isto."),
            Field("open_login_selector", "str", "Seletor CSS do botão que abre o login", "",
                  "O formulário do totalbattle.com já está no HTML quando a página abre, mas "
                  "fechado: é preciso clicar em 'Login' antes de digitar. Vazio = procurar sozinho "
                  "um botão visível com texto de login (botões de Google/Facebook são descartados)."),
            Field("email_selector", "str", "Seletor CSS do campo de e-mail", "",
                  "Vazio = detectar sozinho (input[type=email], name/id contendo email ou login)."),
            Field("password_selector", "str", "Seletor CSS do campo de senha", "",
                  "Vazio = detectar sozinho (input[type=password])."),
            Field("submit_selector", "str", "Seletor CSS do botão de entrar", "",
                  "Vazio = detectar sozinho (button[type=submit] ou botão com texto de login)."),
            Field("logged_in_selector", "str", "Seletor que confirma o login", "",
                  "Elemento que só existe depois de logado, por exemplo 'canvas'. "
                  "Vazio = considerar logado quando o campo de senha desaparecer."),
            Field("wait_after_submit", "float", "Espera após enviar o login (s)", 30.0,
                  "Tempo máximo aguardado pelo carregamento do jogo depois do login.",
                  minimum=5.0, maximum=300.0),
            Field("max_attempts", "int", "Tentativas de login", 2,
                  "Repetições antes de desistir da conta.", minimum=1, maximum=5),
        ],
    ),
    Section(
        "timing",
        "Tempos",
        "Pausas entre ações. Aumente se o jogo estiver lento; diminua para coletar mais rápido.",
        [
            Field("click_delay", "float", "Pausa após um clique (s)", 0.5, "", minimum=0.0, maximum=10.0),
            Field("action_delay", "float", "Pausa entre ações (s)", 0.35, "", minimum=0.0, maximum=10.0),
            Field("chest_click_delay", "float", "Pausa entre baús (s)", 0.25,
                  "Intervalo entre abrir um baú e ler o próximo.", minimum=0.0, maximum=5.0),
            Field("profile_switch_wait", "float", "Espera da troca de perfil (s)", 20.0,
                  "Tempo que o jogo leva para recarregar depois de trocar de cidade.",
                  minimum=1.0, maximum=180.0),
            Field("between_profiles_wait", "float", "Pausa entre perfis (s)", 3.0, "",
                  minimum=0.0, maximum=60.0),
            Field("store_close_wait", "float", "Espera para fechar a loja (s)", 15.0,
                  "Janela de tempo em que a loja é procurada e fechada antes de começar.",
                  minimum=0.0, maximum=120.0),
        ],
    ),
    Section(
        "ocr",
        "OCR",
        "Leitura dos nomes na tela. O RapidOCR (PaddleOCR/ONNX) lê acentos e nomes estrangeiros "
        "que o Tesseract erra.",
        [
            Field("engine", "choice", "Motor de OCR", "auto",
                  "auto = os dois juntos quando ambos estiverem instalados: as letras vêm do "
                  "RapidOCR (bem melhor em nome estrangeiro) e só os acentos vêm do Tesseract. "
                  "Use 'rapidocr' ou 'tesseract' para forçar um só.",
                  choices=("auto", "hybrid", "rapidocr", "tesseract")),
            Field("threads", "int", "Threads do RapidOCR", 4,
                  "Núcleos usados pelo motor neural. Medido nesta máquina: o padrão do "
                  "ONNXRuntime levou 1646 ms por leitura, 4 threads levam 375 ms e 8 pioram "
                  "para 1097 ms — mais threads disputam entre si em vez de somar.",
                  minimum=1, maximum=32),
            Field("capture_scale", "float", "Ampliação da captura", 2.0,
                  "O recorte é renderizado pelo próprio Chrome nesta escala, sem interpolação. "
                  "Medido: 2.0 lê igual a 3.0 e custa metade do tempo de captura; 1.0 já erra.",
                  minimum=1.0, maximum=6.0),
            Field("min_confidence", "float", "Confiança mínima (%)", 45.0,
                  "Leituras abaixo disso são descartadas.", minimum=0.0, maximum=100.0),
            Field("name_match_threshold", "float", "Semelhança mínima do nome", 0.75,
                  "Quanto o nome lido precisa parecer com o configurado para valer como o mesmo perfil.",
                  minimum=0.4, maximum=1.0),
            Field("use_known_names", "bool", "Conferir com os nomes do banco", True,
                  "Compara cada nome lido com o que o banco já conhece. Só corrige diferença de "
                  "formatação (espaço, pontuação, maiúsculas) e o que estiver em "
                  "player_name_mappings — nome parecido NUNCA é fundido com outro, porque uma "
                  "junção indevida não deixa rastro para corrigir. Serve também para pular o "
                  "motor lento quando os três campos já são conhecidos."),
            Field("confidence_gate", "float", "Confiança mínima do Tesseract", 60.0,
                  "Abaixo disso a leitura é conferida com o RapidOCR, que é mais lento. "
                  "Medido: leituras corretas ficam entre 71 e 96; as que ele errou marcaram "
                  "16 e 35. Aumente para conferir mais (mais lento e mais seguro).",
                  minimum=0.0, maximum=100.0),
            Field("tesseract_lang", "str", "Idioma do Tesseract", "por",
                  "Um idioma só. Medido: 'por+eng' custa 446 ms e 'por' sozinho 310 ms, "
                  "com o mesmo resultado — o português já cobre o alfabeto latino."),
            Field("tesseract_path", "path", "Caminho do tesseract.exe", "",
                  "Vazio = procurar no PATH e nos caminhos padrão do Windows."),
        ],
    ),
    Section(
        "vision",
        "Reconhecimento de imagem",
        "Busca dos botões pela imagem de referência gravada na calibração.",
        [
            Field("match_threshold", "float", "Limiar de semelhança", 0.80,
                  "Correlação mínima para considerar que a imagem foi encontrada.",
                  minimum=0.3, maximum=1.0),
            Field("scaled_threshold_relief", "float", "Alívio para escalas diferentes", 0.06,
                  "Desconto no limiar quando a imagem precisa ser redimensionada, já que "
                  "redimensionar sempre reduz um pouco a correlação.",
                  minimum=0.0, maximum=0.3),
            Field("max_attempts", "int", "Tentativas de busca", 3, "", minimum=1, maximum=20),
            Field("retry_interval", "float", "Intervalo entre tentativas (s)", 0.4, "",
                  minimum=0.0, maximum=10.0),
        ],
    ),
]

SECTIONS_BY_KEY = {s.key: s for s in SECTIONS}


def default_config() -> dict:
    """The full configuration with every parameter at its declared default."""
    data = {s.key: {f.key: f.default for f in s.fields} for s in SECTIONS}
    data["accounts"] = []
    return data


def field_for(section_key: str, field_key: str) -> Optional[Field]:
    section = SECTIONS_BY_KEY.get(section_key)
    if not section:
        return None
    for f in section.fields:
        if f.key == field_key:
            return f
    return None

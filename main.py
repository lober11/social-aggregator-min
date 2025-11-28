package com.example.socialaggregator

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel


class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { App() }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun App() {
    val context = LocalContext.current
    val dsm = remember(context) { DataStoreManager(context) }

    // 0 = Исходящие, 1 = Входящие, 2 = Настройки
    var selectedTab by rememberSaveable { mutableStateOf(0) }

    MaterialTheme {
        Surface(
            modifier = Modifier.fillMaxSize(),
            color = MaterialTheme.colorScheme.background
        ) {
            Scaffold(
                topBar = {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(top = 16.dp, bottom = 8.dp)
                    ) {
                        Text(
                            text = stringResource(id = R.string.app_name),
                            style = MaterialTheme.typography.titleLarge,
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp),
                            textAlign = TextAlign.Center
                        )
                        TabRow(selectedTabIndex = selectedTab) {
                            Tab(
                                selected = selectedTab == 0,
                                onClick = { selectedTab = 0 },
                                text = { Text(stringResource(id = R.string.tab_publish)) }
                            )
                            Tab(
                                selected = selectedTab == 1,
                                onClick = { selectedTab = 1 },
                                text = { Text(stringResource(id = R.string.tab_inbox)) }
                            )
                            Tab(
                                selected = selectedTab == 2,
                                onClick = { selectedTab = 2 },
                                text = { Text(stringResource(id = R.string.tab_settings)) }
                            )
                        }
                    }
                }
            ) { innerPadding ->
                Box(
                    modifier = Modifier
                        .padding(innerPadding)
                        .fillMaxSize()
                ) {
                    when (selectedTab) {
                        0 -> PublishRoute(dsm = dsm)
                        1 -> InboxRoute(
                            dsm = dsm,
                            onOpenPublishTab = { selectedTab = 0 }
                        )
                        2 -> SettingsRoute(dsm = dsm)
                    }
                }
            }
        }
    }
}

/** Route-слой для Исходящих */
@Composable
fun PublishRoute(dsm: DataStoreManager) {
    val viewModel: PublishViewModel = viewModel(
        factory = PublishViewModelFactory(dsm)
    )
    val state by viewModel.uiState.collectAsState()

    PublishScreen(
        state = state,
        onMessageChange = viewModel::onMessageChange,
        onSendClick = viewModel::send
    )
}

/** Route для Входящих */
@Composable
fun InboxRoute(
    dsm: DataStoreManager,
    onOpenPublishTab: () -> Unit
) {
    val viewModel: InboxViewModel = viewModel(
        factory = InboxViewModelFactory(dsm)
    )
    val state by viewModel.uiState.collectAsState()

    // авто‑обновление входящих каждые 3 секунды,
    // пока этот composable находится в композиции
    LaunchedEffect(Unit) {
        while (true) {
            viewModel.refreshSilently()   // ← тихое обновление без показа лоадера
            delay(3000)
        }
    }

    var selectedMessage by remember { mutableStateOf<InboxMessageDto?>(null) }
    var chatForEdit by remember { mutableStateOf<Long?>(null) }
    var chatNameDraft by rememberSaveable { mutableStateOf("") }

    val chatSettings =
        (state as? InboxUiState.Success)?.chatSettings ?: emptyMap()

    InboxScreen(
        state = state,
        onRefresh = { viewModel.loadInbox() }, // ручное обновление по свайпу
        onMessageClick = { msg -> selectedMessage = msg },
        onChatLongClick = { chatId ->
            chatForEdit = chatId
            chatNameDraft = chatSettings[chatId]?.customName ?: ""
        }
    )

    selectedMessage?.let { msg ->
        InboxMessageDialog(
            message = msg,
            onDismiss = { selectedMessage = null },
            onMarkRead = {
                viewModel.markAsRead(msg.id)
                selectedMessage = null
            },
            onForwardToPublish = {
                viewModel.forwardToPublish(msg)
                onOpenPublishTab()
                selectedMessage = null
            }
        )
    }

    chatForEdit?.let { chatId ->
        val displayName = chatSettings[chatId]?.customName ?: chatId.toString()
        ChatSettingsDialog(
            chatId = chatId,
            displayName = displayName,
            nameDraft = chatNameDraft,
            onNameChange = { chatNameDraft = it },
            onDismiss = { chatForEdit = null },
            onSaveName = {
                viewModel.renameChat(chatId, chatNameDraft)
                chatForEdit = null
            },
            onMoveUp = { viewModel.moveChatUp(chatId) },
            onMoveDown = { viewModel.moveChatDown(chatId) },
            onMoveFirst = { viewModel.moveChatToFirst(chatId) },
            onMoveLast = { viewModel.moveChatToLast(chatId) }
        )
    }
}

/** Route для Настроек */
@Composable
fun SettingsRoute(dsm: DataStoreManager) {
    val viewModel: SettingsViewModel = viewModel(
        factory = SettingsViewModelFactory(dsm)
    )
    val state by viewModel.uiState.collectAsState()

    SettingsScreen(
        state = state,
        onBaseUrlChange = viewModel::onBaseUrlChange,
        onApiKeyChange = viewModel::onApiKeyChange,
        onIncludeTgChange = viewModel::onIncludeTgChange,
        onTgSourceIdChange = viewModel::onTgSourceIdChange,
        onIncludeVkChange = viewModel::onIncludeVkChange,
        onVkOwnerIdChange = viewModel::onVkOwnerIdChange
    )
}

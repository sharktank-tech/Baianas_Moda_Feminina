TRACKING_STATUS = {
    "aguardando_postagem": {
        "label": "Preparando o envio",
        "description": "Seu pedido está sendo preparado para envio.",
        "step": 1,
    },
    "postado": {
        "label": "Pedido enviado",
        "description": "O pedido foi entregue à transportadora.",
        "step": 2,
    },
    "em_transito": {
        "label": "Em trânsito",
        "description": "O pedido está a caminho do endereço de entrega.",
        "step": 3,
    },
    "saiu_para_entrega": {
        "label": "Saiu para entrega",
        "description": "O pedido deve ser entregue em breve.",
        "step": 4,
    },
    "entregue": {
        "label": "Entregue",
        "description": "O pedido foi entregue.",
        "step": 5,
    },
    "tentativa_entrega": {
        "label": "Tentativa de entrega",
        "description": "A transportadora tentou realizar a entrega.",
        "step": 4,
    },
    "problema_transporte": {
        "label": "Atualização necessária",
        "description": "Estamos verificando uma ocorrência no transporte.",
        "step": 3,
    },
}


def get_tracking_status(status):
    return TRACKING_STATUS.get(
        status,
        {
            "label": "Pedido em acompanhamento",
            "description": "Estamos acompanhando a entrega do seu pedido.",
            "step": 1,
        }
    )
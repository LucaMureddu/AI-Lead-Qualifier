import chromadb

def seed():
    # Connessione al server attivo
    client = chromadb.HttpClient(host='localhost', port=8000)
    collection = client.get_or_create_collection(name="service_catalogue")
    
    # Dati reali di esempio
    services = [
        {"id": "s1", "name": "Web Development", "price": 1500.0, "desc": "Sviluppo siti web e landing page aziendali."},
        {"id": "s2", "name": "Email Server Setup", "price": 300.0, "desc": "Configurazione server di posta e protocolli email."}
    ]
    
    collection.add(
        documents=[s["desc"] for s in services],
        metadatas=[{"name": s["name"], "price": s["price"]} for s in services],
        ids=[s["id"] for s in services]
    )
    print(f"Database popolato con {len(services)} servizi.")

if __name__ == "__main__":
    seed()
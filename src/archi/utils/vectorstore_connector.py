from src.data_manager.vectorstore.postgres_vectorstore import PostgresVectorStore
from src.utils.env import read_secret
from src.utils.logging import get_logger
from src.utils.config_service import ConfigService

logger = get_logger(__name__)


class VectorstoreConnector:
    """
    A class to manage the connection to the PostgreSQL vectorstore with pgvector.
    
    This class initializes the vectorstore parameters from the config
    and provides a method to get the vectorstore connection.
    """

    def __init__(self, config):
        self.config = config
        self._init_vectorstore_params()

    def _init_vectorstore_params(self):
        """
        Initialize the vectorstore parameters from the config.
        """
        dm_config = self.config["data_manager"]

        # Initialize embedding model
        embedding_class_map = ConfigService._resolve_embedding_classes(
            dm_config["embedding_class_map"]
        )

        embedding_name = dm_config["embedding_name"]
        self.embedding_model = embedding_class_map[embedding_name]["class"](
            **embedding_class_map[embedding_name]["kwargs"]
        )
        self.collection_name = dm_config["collection_name"] + "_with_" + embedding_name
        
        self._init_postgres_params()
        
        logger.info(
            f"Vectorstore connection initialized: collection={self.collection_name}"
        )

    def _init_postgres_params(self):
        """Initialize PostgreSQL vectorstore parameters."""
        pg_config = self.config["services"]["postgres"]
        
        self.pg_config = {
            "host": pg_config.get("host", "localhost"),
            "port": pg_config.get("port", 5432),
            "user": pg_config.get("user", "postgres"),
            "password": read_secret("PG_PASSWORD"),
            "dbname": pg_config.get("database", pg_config.get("dbname", "archi")),
        }
        
        # Optional distance metric setting
        vectorstore_config = self.config.get("services", {}).get("vectorstore", {})
        self.distance_metric = vectorstore_config.get("distance_metric", "cosine")

    def _get_postgres_vectorstore(self):
        """
        Create PostgresVectorStore connection.
        """
        vectorstore = PostgresVectorStore(
            pg_config=self.pg_config,
            embedding_function=self.embedding_model,
            collection_name=self.collection_name,
            distance_metric=self.distance_metric,
        )
        
        count = vectorstore.count()
        logger.debug(f"PostgresVectorStore connected: {count} entries in collection")
        return vectorstore

    def get_vectorstore(self):
        """
        Public method to get the vectorstore connection.
        
        Returns the PostgresVectorStore which implements the LangChain VectorStore interface.
        """
        return self._get_postgres_vectorstore()

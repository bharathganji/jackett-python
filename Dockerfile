# Use the official Python image from the Docker Hub
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install the dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Set environment variables
ENV JACKETT_API_URL=${JACKETT_API_URL}
ENV API_KEY=${API_KEY}
ENV PORT=${PORT}

# Expose the port the app runs on
EXPOSE ${PORT}

# Command to run the application
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT --reload"]
